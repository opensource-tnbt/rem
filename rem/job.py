from __future__ import with_statement
import subprocess
import logging
import os
import time
import threading

from callbacks import *
import osspec
import packet


def cut_message(msg, BEG_LEN=None, FIN_LEN=None):
    BEG_LEN = BEG_LEN or 1000
    FIN_LEN = FIN_LEN or 1000
    if msg and len(msg) > BEG_LEN + FIN_LEN + 5:
        msg = msg[:BEG_LEN] + "\n...\n" + msg[-FIN_LEN:]
    return msg or ""


class IResult(Unpickable(type=str,
                         code=int,
                         message=str)):
    def __init__(self, type, code, message):
        self.type = type
        self.code = code
        self.message = message

    def IsSuccessfull(self):
        return self.code == 0

    def IsFailed(self):
        return (self.code is not None) and (self.code != 0)

    def CanRetry(self):
        return True

    def __str__(self):
        return "%s: %s" % (self.type, self.code) + (", \"%s\"" % self.message if self.message else "")


class CommandLineResult(IResult):
    time_format = "%Y/%m/%d %H:%M:%S"

    def __init__(self, code, start_time, fin_time, err, max_err_len=None):
        IResult.__init__(self, "OS exit code", code, "started: %s; finished: %s;%s" \
                                                     % (
            time.strftime(self.time_format, start_time), time.strftime(self.time_format, fin_time),
            "\n" + cut_message(err, max_err_len / 2 if max_err_len else None,
                               max_err_len / 2 if max_err_len else None) if err else ""))


class TriesExceededResult(IResult):
    def __init__(self, maxcount):
        IResult.__init__(self, "The number of attempts exceeded", maxcount, None)

    def CanRetry(self):
        return False


class PackedExecuteResult(IResult):
    def __init__(self, doneCount, allCount):
        if doneCount != allCount:
            IResult.__init__(self, "Unsuccessfull completion of packet work", 1, "%s/%s done" % (doneCount, allCount))
        else:
            IResult.__init__(self, "Successfull completion of packet work", 0, "%s/%s done" % (doneCount, allCount))


class Job(Unpickable(err=nullobject,
                     results=list,
                     tries=int,
                     pipe_fail=bool,
                     description=str,
                     notify_timeout=int,
                     last_update_time=zeroint,
                     working_time=int,
                     _notified=bool),
          CallbackHolder):
    ERR_PENALTY_FACTOR = 6

    def __init__(self, shell, parents, pipe_parents, packetRef, maxTryCount, limitter, max_err_len=None,
                 retry_delay=None, pipe_fail=False, description="", notify_timeout=604800):
        super(Job, self).__init__()
        self.maxTryCount = maxTryCount
        self.limitter = limitter
        self.shell = shell
        self.parents = parents
        self.inputs = pipe_parents
        self.id = id(self)
        self.max_err_len = max_err_len
        self.retry_delay = retry_delay
        self.pipe_fail = pipe_fail
        self.description = description
        self.notify_timeout = notify_timeout
        self.last_update_time = None
        self.working_time = 0
        self._notified = False
        if self.limitter:
            self.AddCallbackListener(self.limitter)
        self.packetRef = packetRef
        self.AddCallbackListener(self.packetRef)

    @staticmethod
    def __read_stream(fh, buffer):
        buffer.append(fh.read())

    @classmethod
    def __wait_process(cls, instance, process, err_pipe):
        out = []
        stderrReadThread = threading.Thread(target=cls.__read_stream, args=(err_pipe, out))
        stderrReadThread.setDaemon(True)
        stderrReadThread.start()
        if process.stdin:
            process.stdin.close()
        working_time = instance.working_time
        last_updated = instance.last_update_time or time.time()
        working_time += time.time() - last_updated
        while process.poll() in None:
            working_time += time.time() - last_updated
            if working_time > instance.notify_timeout:
                instance.UpdateWorkingTime()
            last_updated = time.time()
            if working_time > instance.notify_timeout:
                err = "Job id: %s time limit time limit exceeded"
                result = CommandLineResult(process.poll(), time.time()-working_time, time.time(),\
                                               err, getattr(instance, "max_err_len", None))
                instance._finalize_job_iteration(process, result)
            stderrReadThread.join()
        return "", out[0]

    def __getstate__(self):
        odict = getattr(super(Job, self), "__getstate__", lambda: self.__dict__)()          # copy the dict since we change it
        del odict['last_update_time']              # remove last_update_time entry
        return odict

    def _checkNotificationTime(self):
        if self._notified:
            return
        if self.working_time >= self.notify_timeout:
            msgHelper = packet.PacketCustomLogic(self.packetRef).DoLongExecutionWarning(self)
            SendEmail(self.packetRef.notify_emails, msgHelper)
            self._notified = True

    def UpdateWorkingTime(self):
        now = time.time()
        if self.last_update_time:
            self.working_time += now - self.last_update_time
            self._checkNotificationTime()
        self.last_update_time = now

    def CanStart(self):
        if self.limitter:
            return self.limitter.CanStart()
        return True

    def _finalize_job_iteration(self, process, result):
        if self.pids is not None:
            self.pids.remove(process.pid)
        self.results.append(result)
        if result.IsFailed() and self.tries >= self.maxTryCount and \
                        self.packetRef.state in (packet.PacketState.WORKABLE, packet.PacketState.PENDING):
            self.results.append(TriesExceededResult(self.tries))
            if self.packetRef.kill_all_jobs_on_error:
                self.packetRef.UserSuspend(kill_jobs=True)
                self.packetRef.changeState(packet.PacketState.ERROR)
                logging.info("Job`s %s result: TriesExceededResult", self.id)

    def Run(self, pids=None):
        self.input = self.output = None
        self.errPipe = None
        if pids is None:
            pids = set()
        self.pids = pids
        try:
            self.tries += 1
            self.working_time = 0
            self.FireEvent("start")
            startTime = time.localtime()
            self.errPipe = map(os.fdopen, os.pipe(), 'rw')
            run_args = [osspec.get_shell_location()] + (["-o", "pipefail"] if self.pipe_fail else []) \
                       + ["-c", self.shell]
            process = subprocess.Popen(run_args, stdout=self.output.fileno(), stdin=self.input.fileno(),
                                       stderr=self.errPipe[1].fileno(), close_fds=True, cwd=self.packetRef.directory,
                                       preexec_fn=os.setpgrp)
            if self.pids is not None: self.pids.add(process.pid)
            self.errPipe[1].close()
            _, err = self.__wait_process(self, process, self.errPipe[0])
            result = CommandLineResult(process.poll(), startTime, time.localtime(), err,
                                       getattr(self, "max_err_len", None))
            self._finalize_job_iteration(process, result)
        except Exception, e:
            logging.exception("Run job %s exception: %s", self.id, e)

        finally:
            self.CloseStreams()
            self.FireEvent("done")

    def CloseStreams(self):
        closingStreamGenerators = (
            lambda: self.input,
            lambda: self.output,
            lambda: self.errPipe[0] if self.errPipe else None,
            lambda: self.errPipe[1] if self.errPipe else None
        )
        for fn in closingStreamGenerators:
            try:
                stream = fn()
                if stream is not None:
                    if isinstance(stream, file):
                        if not stream.closed:
                            stream.close()
                    elif isinstance(stream, int):
                        os.close(stream)
                    else:
                        raise RuntimeError("can't close unknown file object %r" % stream)
            except Exception, e:
                logging.exception("%s", e)

    def Terminate(self):
        pids = getattr(self, "pids", None)
        if pids:
            for pid in list(pids):
                osspec.terminate(pid)

    def Result(self):
        return self.results[-1] if self.results else None


class FuncJob(object):
    def __init__(self, runner):
        assert isinstance(runner, FuncRunner), "incorrent arguments for FuncJob initializing"
        self.runner = runner

    def Run(self, pids=None):
        self.runner()
