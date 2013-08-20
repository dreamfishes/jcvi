"""
Codes to submit multiple jobs to JCVI grid engine
"""

import os
import os.path as op
import sys
import re
import logging

from subprocess import Popen, PIPE
from optparse import OptionParser
from multiprocessing import Process

from jcvi.formats.base import write_file
from jcvi.apps.base import ActionDispatcher, sh, popen, backup, debug, \
        set_grid_opts
debug()


PCODE = "04048"  # Project code, JCVI specific


class Jobs (list):
    """
    Runs multiple funcion calls on the SAME computer, using multiprocessing.
    """
    def __init__(self, target, args):

        for x in args:
            self.append(Process(target=target, args=x))

    def run(self):
        for pi in self:
            pi.start()

        for pi in self:
            pi.join()


class GridProcess (object):

    pat1 = re.compile(r"Your job (?P<id>[0-9]*) ")
    pat2 = re.compile(r"Your job-array (?P<id>\S*) ")

    def __init__(self, cmd, jobid="", queue="default", threaded=None,
                       infile=None, outfile=None, errfile=None, arr=None):

        self.cmd = cmd
        self.jobid = jobid
        self.queue = queue
        self.threaded = threaded
        self.infile = infile
        self.outfile = outfile
        self.errfile = errfile
        self.arr = arr
        self.pat = self.pat2 if arr else self.pat1

    def __str__(self):
        return "\t".join((x for x in \
                (self.jobid, self.cmd, self.outfile) if x))

    def build(self):
        # Shell commands
        if "|" in self.cmd or "&&" in self.cmd or "||" in self.cmd:
            quote = "\"" if "'" in self.cmd else "'"
            self.cmd = "sh -c {1}{0}{1}".format(self.cmd, quote)

        # qsub command (the project code is specific to jcvi)
        qsub = "qsub -P {0} -cwd".format(PCODE)
        if self.queue != "default":
            qsub += " -l {0}".format(self.queue)
        if self.threaded:
            qsub += " -pe threaded {0}".format(self.threaded)
        if self.arr:
            assert 1 < self.arr < 100000
            qsub += " -t 1-{0}".format(self.arr)

        # I/O
        infile = self.infile
        outfile = self.outfile
        errfile = self.errfile
        redirect_same = outfile and (outfile == errfile)

        if infile:
            qsub += " -i {0}".format(infile)
        if redirect_same:
            qsub += " -j y"
        if outfile:
            qsub += " -o {0}".format(outfile)
        if errfile and not redirect_same:
            qsub += " -e {0}".format(errfile)

        cmd = " ".join((qsub, self.cmd))
        return cmd

    def start(self):
        cmd = self.build()
        # run the command and get the job-ID (important)
        output = popen(cmd, debug=False).read()

        if output.strip() != "":
            self.jobid = re.search(self.pat, output).group("id")
        else:
            self.jobid = "-1"

        msg = "[{0}] {1}".format(self.jobid, self.cmd)
        if self.infile:
            msg += " < {0} ".format(self.infile)
        if self.outfile:
            backup(self.outfile)
            msg += " > {0} ".format(self.outfile)
        if self.errfile:
            backup(self.errfile)
            msg += " 2> {0} ".format(self.errfile)

        logging.debug(msg)


class Grid (list):

    def __init__(self, cmds, outfiles=[]):

        assert cmds, "Commands empty!"
        if not outfiles:
            outfiles = [None] * len(cmds)

        for cmd, outfile in zip(cmds, outfiles):
            self.append(GridProcess(cmd, outfile=outfile))

    def run(self):

        cwd = os.getcwd()

        for pi in self:
            pi.start()


arraysh = """#!/bin/bash

CMD=`awk "NR==$SGE_TASK_ID" {0}`
$CMD"""


def main():

    actions = (
        ('run', 'run a normal command on grid'),
        ('array', 'run an array job'),
            )

    p = ActionDispatcher(actions)
    p.dispatch(globals())


def array(args):
    """
    %prog array commands.list

    Parallelize a set of commands on grid using array jobs.
    """
    p = OptionParser(array.__doc__)
    set_grid_opts(p)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    cmds, = args
    fp = open(cmds)
    ncmds = sum(1 for x in fp)
    fp.close()

    runfile = "array.sh"
    contents = arraysh.format(cmds)
    write_file(runfile, contents, meta="run script")

    outfile = "\$TASK_ID.out"
    p = GridProcess(runfile, outfile=outfile, errfile=outfile,
                    queue=opts.queue, threaded=opts.threaded,
                    arr=ncmds)
    p.start()


def run(args):
    """
    %prog run command ::: file1 file2

    Parallelize a set of commands on grid. The syntax is modeled after GNU
    parallel <http://www.gnu.org/s/parallel/man.html#options>

    {}   - input line
    {.}  - input line without extension
    {_}  - input line first part
    {/}  - basename of input line
    {/.} - basename of input line without extension
    {/_} - basename of input line first part
    {#}  - sequence number of job to run
    :::  - Use arguments from the command line as input source instead of stdin
    (standard input).

    If file name is `t/example.tar.gz`, then,
    {} is "t/example.tar.gz", {.} is "t/example.tar", {_} is "t/example"
    {/} is "example.tar.gz", {/.} is "example.tar", {/_} is "example"

    A few examples:
    ls -1 *.fastq | %prog run process {} {.}.pdf  # use stdin
    %prog run process {} {.}.pdf ::: *fastq  # use :::
    %prog run "zcat {} > {.}" ::: *.gz  # quote redirection
    %prog run < commands.list  # run a list of commands
    """
    p = OptionParser(run.__doc__)
    set_grid_opts(p)
    opts, args = p.parse_args(args)

    sep = ":::"
    if sep in args:
        sepidx = args.index(sep)
        filenames = args[sepidx + 1:]
        args = args[:sepidx]
        if not filenames:
            filenames = [""]
    else:
        filenames = sys.stdin

    cmd = " ".join(args)

    for i, filename in enumerate(filenames):
        filename = filename.strip()
        noextname = filename.rsplit(".", 1)[0]
        prefix, basename = op.split(filename)
        basenoextname = basename.rsplit(".", 1)[0]
        basefirstname = basename.split(".")[0]
        firstname = op.join(prefix, basefirstname)
        ncmd = cmd

        if "{" in ncmd:
            ncmd = ncmd.replace("{}", filename)
        else:
            ncmd += " " + filename

        ncmd = ncmd.replace("{.}", noextname)
        ncmd = ncmd.replace("{_}", firstname)
        ncmd = ncmd.replace("{/}", basename)
        ncmd = ncmd.replace("{/.}", basenoextname)
        ncmd = ncmd.replace("{/_}", basefirstname)
        ncmd = ncmd.replace("{#}", str(i))

        outfile = None
        if ">" in ncmd:
            ncmd, outfile = ncmd.split(">", 1)
            ncmd, outfile = ncmd.strip(), outfile.strip()

        ncmd = ncmd.strip()
        p = GridProcess(ncmd, outfile=outfile,
                        queue=opts.queue, threaded=opts.threaded)
        p.start()


if __name__ == '__main__':
    main()
