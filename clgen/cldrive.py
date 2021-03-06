#
# Copyright 2016, 2017 Chris Cummins <chrisc.101@gmail.com>.
#
# This file is part of CLgen.
#
# CLgen is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CLgen is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CLgen.  If not, see <http://www.gnu.org/licenses/>.
#
"""
OpenCL kernel driver
"""
from io import StringIO
import numpy as np
import os
import sys

from copy import deepcopy
from functools import partial
from io import open
from labm8 import fs
from labm8 import math as labmath
from numbers import Number
from random import randrange
from six import string_types

import clgen
from clgen import clutil
from clgen import config as cfg

if cfg.USE_OPENCL:
    import pyopencl as cl


class CLDriveException(clgen.CLgenError):
    """
    Module error.
    """
    pass


class OpenCLDriverException(CLDriveException):
    """
    Error from OpenCL driver.
    """
    pass


class OpenCLNotSupported(OpenCLDriverException):
    """
    Thrown if attempting to use OpenCL, but CLgen is configured with OpenCL
    support enabled.
    """
    pass


class OpenCLDeviceNotFound(OpenCLDriverException):
    """
    Could not find a matching device.
    """
    pass


class KernelDriverException(CLDriveException):
    """
    Kernel driver exception.
    """
    pass


class E_BAD_CODE(KernelDriverException):
    """
    Thrown for bad code.
    """
    pass


class E_UGLY_CODE(KernelDriverException):
    """
    Thrown for ugly code.
    """
    pass


class E_BAD_DRIVER(KernelDriverException):
    """
    Thrown in case of CLdrive errors.
    """
    pass


class E_BAD_ARGS(E_BAD_DRIVER):
    """
    Kernel has invalid arguments.
    """
    pass


class E_BAD_PROFILE(E_BAD_DRIVER):
    """
    OpenCL profiling failed.
    """
    pass


class E_NON_TERMINATING(E_BAD_CODE):
    """
    Kernel did not terminate.
    """
    pass


class E_INPUT_INSENSITIVE(E_UGLY_CODE):
    """
    Kernel is input insensitive.
    """
    pass


class E_NO_OUTPUTS(E_UGLY_CODE):
    """
    Kernel does not compute an output.
    """
    pass


class E_NONDETERMINISTIC(E_UGLY_CODE):
    """
    Kernel is nondeterministic.
    """
    pass


def device_type_matches(device, devtype) -> bool:
    """
    Check that device type matches.

    Arguments:
        device (pyopencl.Device): Device.
        devtype (cl.device_info.TYPE): Requested device type.

    Returns:
        bool: True if device is of type devtype, else False.
    """
    actual = device.get_info(cl.device_info.TYPE)
    return actual == devtype


def init_opencl(devtype="__placeholder__", queue_flags=0):
    """
    Initialise an OpenCL for a requested device type.

    Iterates over the available OpenCL platforms and devices looking for a
    device matching the requested type. Constructs and returns an OpenCL
    context and queue for the matching device. Note that OpenCL profiling is
    enabled.

    Arguments:
        devtype (pyopencl.device_type, optional): OpenCL device type.
            Default: gpu.
        queue_flags (cl.command_queue_properties, optional): Bitfield of
            OpenCL queue constructor options.

    Returns:
        (cl.Context, cl.Queue): Tuple of OpenCL context and device queue.

    Raises:
        OpenCLNotSupported: If host does not support OpenCL.
        OpenCLDriverException: In case of error.
        OpenCLDeviceNotFound: If no matching type found.
    """
    if not cfg.USE_OPENCL:
        raise OpenCLNotSupported

    # we have to use a string as a placeholder for the default type or else
    # module import will break when pyopencl is not installed:
    if devtype == "__placeholder__":
        devtype = cl.device_type.GPU

    platforms = cl.get_platforms()
    for platform in platforms:
        ctx = cl.Context(
            properties=[(cl.context_properties.PLATFORM, platform)])
        devices = ctx.get_info(cl.context_info.DEVICES)
        for device in devices:
            if device_type_matches(device, devtype):
                queue_flags |= cl.command_queue_properties.PROFILING_ENABLE
                queue = cl.CommandQueue(ctx, properties=queue_flags)

                return ctx, queue

    raise OpenCLDeviceNotFound("Could not find a suitable device")


def get_event_time(event) -> float:
    """
    Block until OpenCL event has completed and return time delta
    between event submission and end, in milliseconds.

    Arguments:
        event (cl.Event): Event handle.

    Returns:
        float: Elapsed time, in milliseconds.

    Raises:
        E_BAD_PROFILE: In case of error.
    """
    try:
        event.wait()
        tstart = event.get_profiling_info(cl.profiling_info.START)
        tend = event.get_profiling_info(cl.profiling_info.END)
        return (tend - tstart) / 1000000
    except Exception:
        # Possible exceptions:
        #
        #  pyopencl.cffi_cl.RuntimeError:
        #      clwaitforevents failed: OUT_OF_RESOURCES
        #
        raise E_BAD_PROFILE


class KernelPayload(clgen.CLgenObject):
    """
    Abstraction of data for OpenCL kernel.
    """
    def __init__(self, ctx, args: list, ndrange, transfersize: int):
        """
        Create a kernel payload.

        Arguments:
            ctx (cl.Context): OpenCL context.
            args (KernelArg[]): Kernel arguments.
            ndrange (int tuple): NDRange
            transfersize (int): Number of bytes transferred to and from device.
        """
        assert(isinstance(ctx, cl.Context))
        assert(all(isinstance(x, clutil.KernelArg) for x in args))
        assert(all(isinstance(x, int) for x in ndrange))
        assert(isinstance(transfersize, int))

        self._ctx = ctx
        self._args = args
        self._ndrange = ndrange
        self._transfersize = transfersize

    def __deepcopy__(self, memo: dict={}):
        """
        Make a deep copy of a payload.

        This means duplicating all host data, and constructing new
        OpenCL mem objects with pointers to this host data. Note that
        this DOES NOT copy the OpenCL context associated with the
        payload.

        Returns:
            KernelPayload: A new kernel payload instance containing copies of
                all data.
        """
        args = [clutil.KernelArg(a.string) for a in self.args]

        for src, dst in zip(self.args, args):
            if src.hostdata is None and src.is_local:
                # Copy a local memory buffer.
                dst.hostdata = None
                dst.bufsize = src.bufsize
                dst.devdata = cl.LocalMemory(src.bufsize)
            elif src.hostdata is None:
                # Copy a scalar value.
                dst.hostdata = None
                dst.devdata = deepcopy(src.devdata)
            else:
                # Copy a global memory buffer.
                dst.hostdata = deepcopy(src.hostdata, memo=memo)
                dst.flags = src.flags
                dst.devdata = cl.Buffer(self.context, src.flags,
                                        hostbuf=dst.hostdata)

        return KernelPayload(
            self.context, args, self.ndrange, self.transfersize)

    def __eq__(self, other) -> bool:
        """
        Equality comparison. Checks that OpenCL context and arguments match.

        Arguments:
            other (KernelPayload): Other operand.

        Returns:
            bool: True if equal, else false.
        """
        assert(isinstance(other, KernelPayload))

        if self.context != other.context:
            return False

        if len(self.args) != len(other.args):
            return False

        for x, y in zip(self.args, other.args):
            if type(x) != type(y):
                return False
            if x.hostdata is None:
                if x.devdata != y.devdata:
                    return False
            else:
                if len(x.hostdata) != len(y.hostdata):
                    return False
                if any(e1 != e2 for e1, e2 in zip(x.hostdata, y.hostdata)):
                    return False
        return True

    def __ne__(self, other) -> bool:
        """
        Not equal check.

        Arguments:
            other (KernelPayload): Other operand.

        Returns:
            bool: True if not equal, else false.
        """
        return not self.__eq__(other)

    def host_to_device(self, queue) -> float:
        """
        Transfer payload from host to device.

        Arguments:
            queue (cl.Queue): Device Queue.

        Returns:
            float: Elapsed time, in milliseconds.
        """
        assert(isinstance(queue, cl.CommandQueue))
        elapsed = 0

        for arg in self.args:
            if arg.hostdata is None:
                continue

            event = cl.enqueue_copy(queue, arg.devdata, arg.hostdata,
                                    is_blocking=False)
            elapsed += get_event_time(event)

        return elapsed

    def device_to_host(self, queue):
        """
        Transfer payload from device to host.

        Arguments:
            queue (cl.Queue): Device Queue.

        Returns:
            float: Elapsed time, in milliseconds.
        """
        assert(isinstance(queue, cl.CommandQueue))
        elapsed = 0

        for arg in self.args:
            if arg.hostdata is None or arg.is_const:
                continue

            event = cl.enqueue_copy(queue, arg.hostdata, arg.devdata,
                                    is_blocking=False)
            get_event_time(event)

        return elapsed

    @property
    def context(self):
        """ OpenCL context. """
        return self._ctx

    @property
    def args(self):
        """ Argument instances. """
        return self._args

    @property
    def kargs(self):
        """ Device data for arguments. """
        return [a.devdata for a in self.args]

    @property
    def ndrange(self):
        """ ND-range """
        return self._ndrange

    @property
    def transfersize(self):
        """ Returns transfer size (in bytes). """
        return self._transfersize

    def __repr__(self):
        """ Stringify payload. """
        s = "Payload on host:\n"
        for i, arg in enumerate(self.args):
            if arg.hostdata is None:
                s += "  arg {i}: None\n".format(i=i)
            else:
                s += "  arg {i}: {typename} size: {size} {val}\n".format(
                    i=i, typename=type(arg.hostdata).__name__,
                    size=arg.hostdata.size, val=str(arg.hostdata))
        s += "\nPayload on device:\n"
        for i, arg in enumerate(self.args):
            if arg.hostdata is None:
                s += "  arg {i}: {typename} val: {val}\n".format(
                    i=i, typename=type(arg.devdata).__name__,
                    val=str(arg.devdata))
            else:
                s += "  arg {i}: {typename} size: {size} {val}\n".format(
                    i=i, typename=type(arg.devdata).__name__,
                    size=arg.devdata.size, val=str(arg.devdata))
        return s

    @staticmethod
    def _create_payload(nparray, driver, size):
        """
        Create a payload.

        Arguments:
            nparray (function): Numpy array generator.
            driver (KernelDriver): Driver.
            size (int): Payload size parameter.

        Returns:
            KernelPayload: Generated payload.

        Raises:
            E_BAD_ARGS: If payload can't be synthesized for kernel argument(s).
        """
        assert(callable(nparray))
        assert(isinstance(driver, KernelDriver))
        assert(isinstance(size, Number))

        args = [clutil.KernelArg(arg.string) for arg in driver.prototype.args]
        transfer = 0

        try:
            for arg in args:
                arg.hostdata = None

                dtype = arg.numpy_type
                veclength = size * arg.vector_width

                if arg.is_pointer and arg.is_local:
                    # If arg is a pointer to local memory, then we
                    # create a read/write buffer:
                    nonbuf = nparray(veclength)
                    arg.bufsize = nonbuf.nbytes
                    arg.devdata = cl.LocalMemory(arg.bufsize)
                elif arg.is_pointer:
                    # If arg is a pointer to global memory, then we
                    # allocate host memory and populate with values:
                    arg.hostdata = nparray(veclength).astype(dtype)

                    # Determine flags to pass to OpenCL buffer creation:
                    arg.flags = cl.mem_flags.COPY_HOST_PTR
                    if arg.is_const:
                        arg.flags |= cl.mem_flags.READ_ONLY
                    else:
                        arg.flags |= cl.mem_flags.READ_WRITE

                    # Allocate device memory:
                    arg.devdata = cl.Buffer(
                        driver.context, arg.flags, hostbuf=arg.hostdata)

                    # Record transfer overhead. If it's a const buffer,
                    # we're not reading back to host.
                    if arg.is_const:
                        transfer += arg.hostdata.nbytes
                    else:
                        transfer += 2 * arg.hostdata.nbytes
                else:
                    # If arg is not a pointer, then it's a scalar value:
                    arg.devdata = dtype(size)
        except Exception as e:
            raise E_BAD_ARGS(e)

        return KernelPayload(driver.context, args, (size,), transfer)

    @staticmethod
    def create_sequential(driver, size):
        """
        Create a payload of sequential values.

        Arguments:
            driver (KernelDriver): Driver instance.
            size (int): Payload size.

        Returns:
            KernelPayload: Generated payload.

        Raises:
            E_BAD_ARGS: If payload can't be synthesized for kernel argument(s).
        """
        return KernelPayload._create_payload(np.arange, driver, size)

    @staticmethod
    def create_random(driver, size):
        """
        Create a payload of random values.

        Arguments:
            driver (KernelDriver): Driver instance.
            size (int): Payload size.

        Returns:
            KernelPayload: Generated payload.

        Raises:
            E_BAD_ARGS: If payload can't be synthesized for kernel argument(s).
        """
        return KernelPayload._create_payload(np.random.rand, driver, size)


class KernelDriver(clgen.CLgenObject):
    """
    OpenCL Kernel driver. Drives a single OpenCL kernel.
    """
    def __init__(self, ctx, source: str, source_path: str='<stdin>'):
        """
        Arguments:
            ctx (cl.Context): OpenCL context.
            source (str): String kernel source.
            source_path (str, optional): Path to corresponding source file.

        Raises:
            E_BAD_CODE: If source doesn't compile.
            E_UGLY_CODE: If source contains multiple kernels.
        """
        # Safety first, kids:
        assert(type(ctx) == cl.Context)
        assert(isinstance(source, string_types))

        self._ctx = ctx
        self._src = str(source)
        self._program = KernelDriver.build_program(self._ctx, self._src)
        self._prototype = clutil.KernelPrototype.from_source(self._src)

        kernels = self._program.all_kernels()
        if len(kernels) != 1:
            raise E_UGLY_CODE
        self._kernel = kernels[0]
        self._name = self._kernel.get_info(cl.kernel_info.FUNCTION_NAME)

        # Profiling stats
        self._wgsizes = []
        self._transfers = []
        self._runtimes = []

    def __call__(self, queue, payload):
        """
        Run kernel.

        Arguments:
            queue (cl.Queue): Device queue.
            payload (KernelPayload): Input payload.

        Returns:
            Payload: Output payload.

        Raises:
            E_BAD_ARGS: If payload input does not match kernel arguments.
        """
        # Safety first, kids:
        assert(type(queue) == cl.CommandQueue)
        assert(type(payload) == KernelPayload)

        # First off, let's clear any existing tasks in the command
        # queue:
        queue.flush()

        elapsed = 0
        output = deepcopy(payload)

        kargs = output.kargs

        # Copy data from host to device.
        elapsed += output.host_to_device(queue)

        # Try setting the kernel arguments.
        try:
            self.kernel.set_args(*kargs)
        except Exception as e:
            raise E_BAD_ARGS(e)

        # Execute kernel.
        local_size_x = min(output.ndrange[0], 256)
        event = self.kernel(queue, output.ndrange, (local_size_x,), *kargs)
        elapsed += get_event_time(event)

        # Copy data from device to host.
        elapsed += output.device_to_host(queue)

        # Record workgroup size.
        self.wgsizes.append(local_size_x)

        # Record runtime.
        self.runtimes.append(elapsed)

        # Record transfers.
        self.transfers.append(payload.transfersize)

        # Check that everything is done before we finish:
        queue.flush()

        return output

    def __repr__(self) -> str:
        return self.source

    def validate(self, queue, size: int=16):
        """
        Run dynamic checker on OpenCL kernel.

        This is a 3 step process:
          1. Create 4 equal size payloads A1in, B1in, A2in, B2in, subject to
             restrictions: A1in = A2in , B1in = B2in , A1in != B1in.
          2. Execute kernel k 4 times: k(A1in) → A1out, k(B1in) → B1out,
             k(A2in) → A2out, k(B2in) → B2out.
          3. Assert:
             a. A1out != A1in or B1out != B1in, else k has no output.
             b. A1out == A2out and B1out == B2out, else k is nondeterministic.
             c. A1out != B1out, else k is input insensitive.

        Raises:
            E_BAD_DRIVER: If unable to synthesize payloads (see step 1 above).
            E_NO_OUTPUTS: If kernel has no outputs (step 3a above).
            E_NONDETERMINISTIC: If kernel is nondeterministic (step 3b above).
            E_INPUT_INSENSITIVE: If kernel is input insensitive (step 3c above).
        """
        assert(type(queue) == cl.CommandQueue)

        def assert_constraint(constraint, err=CLDriveException, msg=None):
            """ assert condition else raise exception with message """
            if not constraint:
                raise err(msg)

        # Create payloads.
        A1in = KernelPayload.create_sequential(self, size)
        A2in = deepcopy(A1in)

        B1in = KernelPayload.create_random(self, size)
        while B1in == A1in:  # input constraint: A1in != B1in
            B1in = KernelPayload.create_random(self, size)
        B2in = deepcopy(B1in)

        # Input constraints.
        assert_constraint(A1in == A2in, E_BAD_DRIVER, "A1in != A2in")
        assert_constraint(B1in == B2in, E_BAD_DRIVER, "B1in != B2in")

        # Run kernel.
        k = partial(self, queue)
        A1out = k(A1in)
        B1out = k(B1in)
        A2out = k(A2in)
        B2out = k(B2in)

        # outputs must be different from inputs:
        assert_constraint(A1in != A1out, E_NO_OUTPUTS)
        assert_constraint(B1in != B1out, E_NO_OUTPUTS)

        # outputs must be consistent across runs:
        assert_constraint(A1out == A2out, E_NONDETERMINISTIC)
        assert_constraint(B1out == B2out, E_NONDETERMINISTIC)

        # outputs must depend on inputs:
        if any(not x.is_const for x in self.prototype.args):
            assert_constraint(A1out != B1out, E_INPUT_INSENSITIVE)

    def profile(self, queue, size: int=16, must_validate: bool=False,
                out=sys.stdout, metaout=sys.stderr, min_num_iterations: int=10):
        """
        Run kernel and profile runtime.

        Output format (CSV):

            out:      <kernel> <wgsize> <transfer> <runtime> <ci>
            metaout:  <error> <kernel>
        """
        assert(isinstance(queue, cl.CommandQueue))

        if must_validate:
            try:
                self.validate(queue, size)
            except CLDriveException as e:
                print(type(e).__name__, self.name, sep=',', file=metaout)

        P = KernelPayload.create_random(self, size)
        k = partial(self, queue)

        while len(self.runtimes) < min_num_iterations:
            k(P)

        wgsize = int(round(labmath.mean(self.wgsizes)))
        transfer = int(round(labmath.mean(self.transfers)))
        mean = labmath.mean(self.runtimes)
        ci = labmath.confinterval(self.runtimes, array_mean=mean)[1] - mean
        print(self.name, wgsize, transfer, round(mean, 6), round(ci, 6),
              sep=',', file=out)

    @property
    def context(self):
        """ OpenCL context. """
        return self._ctx

    @property
    def source(self):
        """ Kernel source code. """
        return self._src

    @property
    def program(self):
        """ OpenCL program instance. """
        return self._program

    @property
    def prototype(self):
        """ Kernel prototype instance. """
        return self._prototype

    @property
    def kernel(self):
        """ Kernel instance. """
        return self._kernel

    @property
    def name(self):
        """ Kernel name. """
        return self._name

    @property
    def wgsizes(self):
        """ Workgroup sizes used. """
        return self._wgsizes

    @property
    def transfers(self):
        """ Size of data transfers (in bytes). """
        return self._transfers

    @property
    def runtimes(self):
        """ Runtimes recorded. """
        return self._runtimes

    @staticmethod
    def build_program(ctx, src, quiet=True):
        """
        Compile an OpenCL program.

        Arguments:
            ctx (cl.Context): OpenCL context.
            src (str): Kernel source.
            quiet (bool, optional): suppress compiler output.

        Returns:
            cl.Program: Program instance.

        Raises:
            E_BAD_CODE: If does not build.
        """
        if quiet:
            os.environ['PYOPENCL_COMPILER_OUTPUT'] = '0'
        else:
            os.environ['PYOPENCL_COMPILER_OUTPUT'] = '1'

        try:
            return cl.Program(ctx, src).build()
        except Exception as e:
            raise E_BAD_CODE(e)


def kernel(src, filename: str='<stdin>', devtype="__placeholder__",
           size: int=None, must_validate: bool=False, fatal_errors: bool=False):
    """
    Drive a kernel.

    Output format (CSV):

        out:      <file> <size> <kernel> <wgsize> <transfer> <runtime> <ci>
        metaout:  <file> <size> <error> <kernel>
    """
    # we have to use a string as a placeholder for the default type or else
    # module import will break when pyopencl is not installed:
    if devtype == "__placeholder__":
        devtype = cl.device_type.GPU

    try:
        ctx, queue = init_opencl(devtype=devtype)
        driver = KernelDriver(ctx, src)
    except Exception as e:
        if fatal_errors:
            raise e
        print(filename, size, type(e).__name__, '-', sep=',', file=sys.stderr)
        return

    # If no size is given, pick a random size.
    if size is None:
        size = 2 ** randrange(4, 15)

    out = StringIO()
    metaout = StringIO()
    driver.profile(queue, size=size, must_validate=must_validate,
                   out=out, metaout=metaout)

    stdout = out.getvalue()
    stderr = metaout.getvalue()

    # Print results:
    [print(filename, size, line, sep=',')
     for line in stdout.split('\n') if line]
    [print(filename, size, line, sep=',', file=sys.stderr)
     for line in stderr.split('\n') if line]


def file(path: str, **kwargs):
    """
    Drive an OpenCL kernel file.

    Arguments:
        path (str): Path to file
        **kwargs (dict, optional): Arguments to kernel()
    """
    with open(path) as infile:
        src = infile.read()
        kernels = clutil.get_cl_kernels(src)

        # error if there's no kernels
        if not len(kernels):
            if kwargs.get("fatal_errors", False):
                raise E_BAD_CODE("no kernels in file '{}'".format(path))
            else:
                print(path, "-", "E_BAD_CODE", '-', sep=',', file=sys.stderr)

        # execute all kernels in file
        for kernelsrc in kernels:
            kernel(kernelsrc, filename=fs.basename(path), **kwargs)
