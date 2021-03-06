from collections import defaultdict
import functools
import inspect
from textwrap import dedent
import types

from mako.template import Template
import numpy as np

from pysph.cpy.opencl import profile_kernel
from pysph.cpy.config import get_config
from pysph.cpy.translator import OpenCLConverter
from .equation import get_array_names
from .integrator_cython_helper import IntegratorCythonHelper
from .acceleration_eval_opencl_helper import (get_kernel_definition,
                                              wrap_code)


class OpenCLIntegrator(object):
    """Does the actual work of calling the kernels for integration.
    """
    def __init__(self, helper, c_acceleration_eval):
        self.helper = helper
        self.acceleration_eval = c_acceleration_eval
        self.nnps = None
        self.parallel_manager = None
        self._post_stage_callback = None
        self._use_double = get_config().use_double
        self._setup_methods()

    def _setup_methods(self):
        """This sets up a few methods of this class.

        This is unfortunately a bit hacky right now and should be cleaned
        later.

        It creates the methods for the following:

        self.one_timestep: this is the same as the integrator's method.

        self.initialize, self.stage1 ... self.stagen are created based on the
        number of steppers.

        """
        code = self.helper.get_timestep_code()
        ns = {}
        exec(code, ns)
        self.one_timestep = types.MethodType(ns['one_timestep'], self)

        for method in self.helper.get_stepper_method_wrapper_names():
            setattr(self, method, functools.partial(self._do_stage, method))

    def _do_stage(self, method):
        # Call the appropriate kernels for either initialize/stage computation.
        call_info = self.helper.calls[method]
        py_call_info = self.helper.py_calls['py_' + method]
        dtype = np.float64 if self._use_double else np.float32
        extra_args = [np.asarray(self.t, dtype=dtype),
                      np.asarray(self.dt, dtype=dtype)]
        # Call the py_{method} for each destination.
        for name, (py_meth, dest) in py_call_info.items():
            py_meth(dest, *extra_args)

        # Call the stage* method for each destination.
        for name, (call, args, dest) in call_info.items():
            n = dest.get_number_of_particles(real=True)
            args[1] = (n,)
            # Compute the remaining arguments.
            rest = [x() for x in args[3:]]
            call(*(args[:3] + rest + extra_args))

    def set_nnps(self, nnps):
        self.nnps = nnps

    def set_parallel_manager(self, pm):
        self.parallel_manager = pm

    def set_post_stage_callback(self, callback):
        self._post_stage_callback = callback

    def compute_accelerations(self):
        # update NNPS since particles have moved
        if self.parallel_manager:
            self.parallel_manager.update()
        self.nnps.update()

        # Evaluate
        self.acceleration_eval.compute(self.t, self.dt)

    def do_post_stage(self, stage_dt, stage):
        """This is called after every stage of the integrator.

        Internally, this calls any post_stage_callback function that has
        been given to take suitable action.

        Parameters
        ----------

         - stage_dt : double: the timestep taken at this stage.

         - stage : int: the stage completed (starting from 1).
        """
        self.t = self.orig_t + stage_dt
        if self._post_stage_callback is not None:
            self._post_stage_callback(self.t, self.dt, stage)

    def step(self, t, dt):
        """Main step routine.
        """
        self.orig_t = t
        self.t = t
        self.dt = dt
        self.one_timestep(t, dt)


class IntegratorOpenCLHelper(IntegratorCythonHelper):
    def __init__(self, integrator, acceleration_eval_helper):
        super(IntegratorOpenCLHelper, self).__init__(
            integrator, acceleration_eval_helper
        )
        self.py_data = defaultdict(dict)
        self.data = defaultdict(dict)
        self.py_calls = defaultdict(dict)
        self.calls = defaultdict(dict)
        self.program = None

    def _setup_call_data(self):
        array_map = self.acceleration_eval_helper._array_map
        q = self.acceleration_eval_helper._queue
        calls = self.calls
        py_calls = self.py_calls
        steppers = self.object.steppers
        for method, info in self.py_data.items():
            for dest_name in info:
                py_meth = getattr(steppers[dest_name], method)
                dest = array_map[dest_name]
                py_calls[method][dest] = (py_meth, dest)

        for method, info in self.data.items():
            for dest_name, (kernel, args) in info.items():
                dest = array_map[dest_name]

                # Note: This is done to do some late binding. Instead of
                # just directly storing the dest.gpu.x, we compute it on
                # the fly as the number of particles and the actual buffer
                # may change.
                def _getter(dest_gpu, x):
                    return getattr(dest_gpu, x).dev.data

                _args = [
                    functools.partial(_getter, dest.gpu, x[2:])
                    for x in args
                ]
                all_args = [q, None, None] + _args
                call = getattr(self.program, kernel)
                call = profile_kernel(call, call.function_name)
                calls[method][dest] = (call, all_args, dest)

    def get_code(self):
        if self.object is not None:
            tpl = dedent("""
            // ------------------------------------------------------------
            // Integrator steppers.
            ${helper.get_stepper_code()}

            // ------------------------------------------------------------
            % for dest in sorted(helper.object.steppers.keys()):
            // Steppers for ${dest}
            % for method in helper.get_stepper_method_wrapper_names():
            <% helper.get_py_stage_code(dest, method) %>
            % if helper.has_stepper_loop(dest, method):
            ${helper.get_stepper_kernel(dest, method)}
            % endif
            % endfor
            % endfor
            // ------------------------------------------------------------
            """)
            template = Template(text=tpl)
            return template.render(helper=self)
        else:
            return ''

    def setup_compiled_module(self, module, acceleration_eval):
        # Create the compiled module.
        self.program = module
        self._setup_call_data()
        cython_integrator = OpenCLIntegrator(self, acceleration_eval)
        # Setup the integrator to use this compiled module.
        self.object.set_compiled_object(cython_integrator)

    def get_py_stage_code(self, dest, method):
        stepper = self.object.steppers[dest]
        method = 'py_' + method
        if hasattr(stepper, method):
            self.py_data[method][dest] = dest

    def get_timestep_code(self):
        method = self.object.one_timestep
        return dedent(''.join(inspect.getsourcelines(method)[0]))

    def get_stepper_code(self):
        classes = {}
        for dest, stepper in self.object.steppers.items():
            cls = stepper.__class__.__name__
            classes[cls] = stepper

        known_types = dict(self.acceleration_eval_helper.known_types)
        code_gen = OpenCLConverter(known_types=known_types)

        wrappers = []
        for cls in sorted(classes.keys()):
            wrappers.append(code_gen.parse_instance(classes[cls]))
        return '\n'.join(wrappers)

    def get_stepper_kernel(self, dest, method):
        kernel = '{method}_{dest}'.format(dest=dest, method=method)
        stepper = self.object.steppers.get(dest)
        cls = stepper.__class__.__name__
        args = self.get_args(dest, method)
        if 'self' in args:
            args.remove('self')
        s, d = get_array_names(args)

        all_args = self.acceleration_eval_helper._get_typed_args(
            list(d) + ['t', 'dt']
        )

        # All the steppers are essentially empty structs so we just pass 0 as
        # the stepper struct as it is not used at all. This simplifies things
        # as we do not need to generate structs and pass them around.
        code = [
            'int d_idx = get_global_id(0);',
        ] + wrap_code(
            '{cls}_{method}({args});'.format(
                cls=cls, method=method,
                args=', '.join(['0'] + args)
            ), indent=''
        )

        body = '\n'.join(' '*4 + x for x in code)

        self.data[method][dest] = (kernel, list(d))

        sig = get_kernel_definition(kernel, all_args)
        return (
            '{sig}\n{{\n{body}\n}}\n'.format(
                sig=sig, body=body
            )
        )
