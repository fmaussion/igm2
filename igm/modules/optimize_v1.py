#!/usr/bin/env python3

"""
Copyright (C) 2021-2023 Guillaume Jouvet <guillaume.jouvet@unil.ch>
Published under the GNU GPL (Version 3), check at the LICENSE file
"""

import numpy as np
import os
import matplotlib.pyplot as plt
import datetime, time
import math
import tensorflow as tf
from scipy import stats
from netCDF4 import Dataset

from igm.modules.utils import *
from igm.modules.iceflow_v1 import *


def params_optimize_v1(parser):
    parser.add_argument(
        "--opti_vars_to_save",
        type=list,
        default=[
            "topg",
            "usurf",
            "thk",
            "strflowctrl",
            "arrhenius",
            "slidingco",
            "velsurf_mag",
            "velsurfobs_mag",
            "divflux",
        ],
        help="List of variables to be recorded in the ncdef file",
    )

    parser.add_argument(
        "--opti_thr_strflowctrl",
        type=float,
        default=78.0,
        help="threshold value for strflowctrl",
    )
    parser.add_argument(
        "--opti_init_zero_thk",
        type=str2bool,
        default="False",
        help="Initialize the optimization with zero ice thickness",
    )
    parser.add_argument(
        "--opti_regu_param_thk",
        type=float,
        default=10.0,
        help="Regularization weight for the ice thickness in the optimization",
    )
    parser.add_argument(
        "--opti_regu_param_strflowctrl",
        type=float,
        default=1.0,
        help="Regularization weight for the strflowctrl field in the optimization",
    )
    parser.add_argument(
        "--opti_smooth_anisotropy_factor",
        type=float,
        default=0.2,
        help="Smooth anisotropy factor for the ice thickness regularization in the optimization",
    )
    parser.add_argument(
        "--opti_convexity_weight",
        type=float,
        default=0.002,
        help="Convexity weight for the ice thickness regularization in the optimization",
    )
    parser.add_argument(
        "--opti_usurfobs_std",
        type=float,
        default=5.0,
        help="Confidence/STD of the top ice surface as input data for the optimization",
    )
    parser.add_argument(
        "--opti_strflowctrl_std",
        type=float,
        default=5.0,
        help="Confidence/STD of strflowctrl",
    )
    parser.add_argument(
        "--opti_velsurfobs_std",
        type=float,
        default=3.0,
        help="Confidence/STD of the surface ice velocities as input data for the optimization (if 0, velsurfobs_std field must be given)",
    )
    parser.add_argument(
        "--opti_thkobs_std",
        type=float,
        default=5.0,
        help="Confidence/STD of the ice thickness profiles (unless given)",
    )
    parser.add_argument(
        "--opti_divfluxobs_std",
        type=float,
        default=1.0,
        help="Confidence/STD of the flux divergence as input data for the optimization (if 0, divfluxobs_std field must be given)",
    )
    parser.add_argument(
        "--opti_control",
        type=list,
        default=["thk", "strflowctrl", "usurf"],
        help="List of optimized variables for the optimization",
    )
    parser.add_argument(
        "--opti_cost",
        type=list,
        default=["velsurf", "thk", "usurf", "divfluxfcz", "icemask"],
        help="List of cost components for the optimization",
    )
    parser.add_argument(
        "--opti_nbitmin",
        type=int,
        default=50,
        help="Min iterations for the optimization",
    )
    parser.add_argument(
        "--opti_nbitmax",
        type=int,
        default=1000,
        help="Max iterations for the optimization",
    )
    parser.add_argument(
        "--opti_step_size",
        type=float,
        default=0.001,
        help="Step size for the optimization",
    )
    parser.add_argument(
        "--opti_output_freq",
        type=int,
        default=50,
        help="Frequency of the output for the optimization",
    )
    parser.add_argument(
        "--geology_optimized_file",
        type=str,
        default="geology-optimized.nc",
        help="Geology input file",
    )


def init_optimize_v1(params, self):
    """
    This function does the data assimilation (inverse modelling) to optimize thk, strflowctrl ans usurf from data
    Check at this [page](https://github.com/jouvetg/igm/blob/main/doc/Inverse-modeling.md)
    """

    init_iceflow_v1(params, self)

    ###### PERFORM CHECKS PRIOR OPTIMIZATIONS

    # make sure this condition is satisfied
    assert ("usurf" in params.opti_cost) == ("usurf" in params.opti_control)

    # make sure the loaded ice flow emulator has these inputs
    assert (
        self.iceflow_mapping["fieldin"]
        == ["thk", "slopsurfx", "slopsurfy", "arrhenius", "slidingco"]
    ) | (
        self.iceflow_mapping["fieldin"]
        == ["thk", "slopsurfx", "slopsurfy", "strflowctrl"]
    )

    # make sure the loaded ice flow emulator has at least these outputs
    assert all(
        [
            (f in self.iceflow_mapping["fieldout"])
            for f in ["ubar", "vbar", "uvelsurf", "vvelsurf"]
        ]
    )

    # make sure that there are lease some profiles in thkobs
    if "thk" in params.opti_cost:
        assert not tf.reduce_all(tf.math.is_nan(self.thkobs))

    ###### PREPARE DATA PRIOR OPTIMIZATIONS

    if hasattr(self, "uvelsurfobs") & hasattr(self, "vvelsurfobs"):
        self.velsurfobs = tf.stack([self.uvelsurfobs, self.vvelsurfobs], axis=-1)

    if "divfluxobs" in params.opti_cost:
        self.divfluxobs = self.smb - self.dhdt

    if not params.opti_smooth_anisotropy_factor == 1:
        compute_flow_direction_for_anisotropic_smoothing(self)

    if hasattr(self, "thkinit"):
        self.thk = self.thkinit
    else:
        self.thk = tf.zeros_like(self.thk)

    if params.opti_init_zero_thk:
        self.thk = tf.zeros_like(self.thk)

    ###### PREPARE OPIMIZER

    if int(tf.__version__.split(".")[1]) <= 10:
        optimizer = tf.keras.optimizers.Adam(learning_rate=params.opti_step_size)
    else:
        optimizer = tf.keras.optimizers.legacy.Adam(learning_rate=params.opti_step_size)

    # initial_learning_rate * decay_rate ^ (step / decay_steps)
    # lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay( initial_learning_rate=opti_step_size, decay_steps=100, decay_rate=0.9)
    # optimizer = tf.keras.optimizers.Adam(learning_rate=lr_schedule)

    # add scalng for usurf
    self.iceflow_fieldbounds["usurf"] = self.iceflow_fieldbounds["slopsurfx"] * self.dx

    ###### PREPARE VARIABLES TO OPTIMIZE

    if self.iceflow_mapping["fieldin"] == [
        "thk",
        "slopsurfx",
        "slopsurfy",
        "arrhenius",
        "slidingco",
    ]:
        self.iceflow_fieldbounds["strflowctrl"] = (
            self.iceflow_fieldbounds["arrhenius"]
            + self.iceflow_fieldbounds["slidingco"]
        )

    thk = tf.Variable(self.thk / self.iceflow_fieldbounds["thk"])  # normalized vars
    strflowctrl = tf.Variable(
        self.strflowctrl / self.iceflow_fieldbounds["strflowctrl"]
    )  # normalized vars
    usurf = tf.Variable(
        self.usurf / self.iceflow_fieldbounds["usurf"]
    )  # normalized vars

    self.costs = []

    self.tcomp["optimize_v1"] = []

    # main loop
    for i in range(params.opti_nbitmax):
        with tf.GradientTape() as t:
            self.tcomp["optimize_v1"].append(time.time())

            # is necessary to remember all operation to derive the gradients w.r.t. control variables
            if "thk" in params.opti_control:
                t.watch(thk)
            if "usurf" in params.opti_control:
                t.watch(usurf)
            if "strflowctrl" in params.opti_control:
                t.watch(strflowctrl)

            # update surface gradient
            if (i == 0) | ("usurf" in params.opti_control):
                slopsurfx, slopsurfy = compute_gradient_tf(
                    usurf * self.iceflow_fieldbounds["usurf"], self.dx, self.dx
                )
                slopsurfx = slopsurfx / self.iceflow_fieldbounds["slopsurfx"]
                slopsurfy = slopsurfy / self.iceflow_fieldbounds["slopsurfy"]

            if self.iceflow_mapping["fieldin"] == [
                "thk",
                "slopsurfx",
                "slopsurfy",
                "arrhenius",
                "slidingco",
            ]:
                thrv = (
                    params.opti_thr_strflowctrl
                    / self.iceflow_fieldbounds["strflowctrl"]
                )
                arrhenius = tf.where(strflowctrl <= thrv, strflowctrl, thrv)
                slidingco = tf.where(strflowctrl <= thrv, 0, strflowctrl - thrv)

                # build input of the emulator
                X = tf.concat(
                    [
                        tf.expand_dims(
                            tf.expand_dims(tf.pad(thk, self.PAD, "CONSTANT"), axis=0),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(slopsurfx, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(slopsurfy, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(arrhenius, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(slidingco, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                    ],
                    axis=-1,
                )

            elif self.iceflow_mapping["fieldin"] == [
                "thk",
                "slopsurfx",
                "slopsurfy",
                "strflowctrl",
            ]:
                # build input of the emulator
                X = tf.concat(
                    [
                        tf.expand_dims(
                            tf.expand_dims(tf.pad(thk, self.PAD, "CONSTANT"), axis=0),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(slopsurfx, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(slopsurfy, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                        tf.expand_dims(
                            tf.expand_dims(
                                tf.pad(strflowctrl, self.PAD, "CONSTANT"), axis=0
                            ),
                            axis=-1,
                        ),
                    ],
                    axis=-1,
                )
            else:
                # ONLY these 2 above cases were implemented !!!
                sys.exit("CHANGE THE ICE FLOW EMULATOR -- IMCOMPATIBLE FOR INVERSION ")

            # evalutae th ice flow emulator
            Y = self.iceflow_model(X)

            # get the dimensions of the working array
            Ny, Nx = self.thk.shape

            # save output variables into self.variables for outputs
            for kk, f in enumerate(self.iceflow_mapping["fieldout"]):
                vars(self)[f] = Y[0, :Ny, :Nx, kk] * self.iceflow_fieldbounds[f]

            # find index of variables in output
            iubar = self.iceflow_mapping["fieldout"].index("ubar")
            ivbar = self.iceflow_mapping["fieldout"].index("vbar")
            iuvsu = self.iceflow_mapping["fieldout"].index("uvelsurf")
            ivvsu = self.iceflow_mapping["fieldout"].index("vvelsurf")

            # save output of the emaultor to compute the costs function
            ubar = (
                Y[0, :Ny, :Nx, iubar] * self.iceflow_fieldbounds["ubar"]
            )  # NOT normalized vars
            vbar = (
                Y[0, :Ny, :Nx, ivbar] * self.iceflow_fieldbounds["vbar"]
            )  # NOT normalized vars
            uvelsurf = (
                Y[0, :Ny, :Nx, iuvsu] * self.iceflow_fieldbounds["uvelsurf"]
            )  # NOT normalized vars
            vvelsurf = (
                Y[0, :Ny, :Nx, ivvsu] * self.iceflow_fieldbounds["vvelsurf"]
            )  # NOT normalized vars
            velsurf = tf.stack([uvelsurf, vvelsurf], axis=-1)  # NOT normalized vars

            # misfit between surface velocity
            if "velsurf" in params.opti_cost:
                ACT = ~tf.math.is_nan(self.velsurfobs)
                COST_U = 0.5 * tf.reduce_mean(
                    ((self.velsurfobs[ACT] - velsurf[ACT]) / params.opti_velsurfobs_std)
                    ** 2
                )
            else:
                COST_U = tf.Variable(0.0)

            # misfit between ice thickness profiles
            if "thk" in params.opti_cost:
                ACT = ~tf.math.is_nan(self.thkobs)
                COST_H = 0.5 * tf.reduce_mean(
                    (
                        (self.thkobs[ACT] - thk[ACT] * self.iceflow_fieldbounds["thk"])
                        / params.opti_thkobs_std
                    )
                    ** 2
                )
            else:
                COST_H = tf.Variable(0.0)

            # misfit divergence of the flux
            if ("divfluxobs" in params.opti_cost) | ("divfluxfcz" in params.opti_cost):
                divflux = compute_divflux(
                    ubar,
                    vbar,
                    thk * self.iceflow_fieldbounds["thk"],
                    self.dx,
                    self.dx,
                )

                if "divfluxfcz" in params.opti_cost:
                    ACT = self.icemaskobs > 0.5
                    if i % 10 == 0:
                        # his does not need to be comptued any iteration as this is expensive
                        res = stats.linregress(
                            self.usurf[ACT], divflux[ACT]
                        )  # this is a linear regression (usually that's enough)
                    # or you may go for polynomial fit (more gl, but may leads to errors)
                    #  weights = np.polyfit(self.usurf[ACT],divflux[ACT], 2)
                    divfluxtar = tf.where(
                        ACT, res.intercept + res.slope * self.usurf, 0.0
                    )
                #                        divfluxtar = tf.where(ACT, np.poly1d(weights)(self.usurf) , 0.0 )

                else:
                    divfluxtar = self.divfluxobs

                ACT = self.icemaskobs > 0.5
                COST_D = 0.5 * tf.reduce_mean(
                    ((divfluxtar[ACT] - divflux[ACT]) / params.opti_divfluxobs_std) ** 2
                )

            else:
                COST_D = tf.Variable(0.0)

            # misfit between top ice surfaces
            if "usurf" in params.opti_cost:
                ACT = self.icemaskobs > 0.5
                COST_S = 0.5 * tf.reduce_mean(
                    (
                        (
                            usurf[ACT] * self.iceflow_fieldbounds["usurf"]
                            - self.usurfobs[ACT]
                        )
                        / params.opti_usurfobs_std
                    )
                    ** 2
                )
            else:
                COST_S = tf.Variable(0.0)

            # force usurf = usurf - topg
            if "topg" in params.opti_cost:
                ACT = self.icemaskobs == 1
                COST_T = 10**10 * tf.reduce_mean(
                    (
                        usurf[ACT] * self.iceflow_fieldbounds["usurf"]
                        - thk[ACT] * self.iceflow_fieldbounds["thk"]
                        - self.topg[ACT]
                    )
                    ** 2
                )
            else:
                COST_T = tf.Variable(0.0)

            # force zero thikness outisde the mask
            if "icemask" in params.opti_cost:
                COST_O = 10**10 * tf.math.reduce_mean(
                    tf.where(self.icemaskobs > 0.5, 0.0, thk**2)
                )
            else:
                COST_O = tf.Variable(0.0)

            # Here one enforces non-negative ice thickness, and possibly zero-thickness in user-defined ice-free areas.
            if "thk" in params.opti_control:
                COST_HPO = 10**10 * tf.math.reduce_mean(
                    tf.where(thk >= 0, 0.0, thk**2)
                )
            else:
                COST_HPO = tf.Variable(0.0)

            # # Make sur to keep reasonable values for strflowctrl
            if "strflowctrl" in params.opti_control:
                COST_STR = 0.5 * tf.reduce_mean(
                    (
                        (
                            strflowctrl * self.iceflow_fieldbounds["strflowctrl"]
                            - params.opti_thr_strflowctrl
                        )
                        / params.opti_strflowctrl_std
                    )
                    ** 2
                )
            else:
                COST_STR = tf.Variable(0.0)

            # Here one adds a regularization terms for the ice thickness to the cost function
            if "thk" in params.opti_control:
                if params.opti_smooth_anisotropy_factor == 1:
                    dbdx = thk[:, 1:] - thk[:, :-1]
                    dbdy = thk[1:, :] - thk[:-1, :]
                    REGU_H = params.opti_regu_param_thk * (
                        tf.nn.l2_loss(dbdx) + tf.nn.l2_loss(dbdy)
                    )
                else:
                    dbdx = thk[:, 1:] - thk[:, :-1]
                    dbdx = (dbdx[1:, :] + dbdx[:-1, :]) / 2.0
                    dbdy = thk[1:, :] - thk[:-1, :]
                    dbdy = (dbdy[:, 1:] + dbdy[:, :-1]) / 2.0
                    REGU_H = params.opti_regu_param_thk * (
                        tf.nn.l2_loss((dbdx * self.flowdirx + dbdy * self.flowdiry))
                        + params.opti_smooth_anisotropy_factor
                        * tf.nn.l2_loss((dbdx * self.flowdiry - dbdy * self.flowdirx))
                        - params.opti_convexity_weight * tf.math.reduce_sum(thk)
                    )
            else:
                REGU_H = tf.Variable(0.0)

            # Here one adds a regularization terms for strflowctrl to the cost function
            if "strflowctrl" in params.opti_control:
                dadx = tf.math.abs(strflowctrl[:, 1:] - strflowctrl[:, :-1])
                dady = tf.math.abs(strflowctrl[1:, :] - strflowctrl[:-1, :])
                dadx = tf.where(
                    (self.icemaskobs[:, 1:] > 0.5) & (self.icemaskobs[:, :-1] > 0.5),
                    dadx,
                    0.0,
                )
                dady = tf.where(
                    (self.icemaskobs[1:, :] > 0.5) & (self.icemaskobs[:-1, :] > 0.5),
                    dady,
                    0.0,
                )
                REGU_A = params.opti_regu_param_strflowctrl * (
                    tf.nn.l2_loss(dadx) + tf.nn.l2_loss(dady)
                )
            else:
                REGU_A = tf.Variable(0.0)

            # sum all component into the main cost function
            COST = (
                COST_U
                + COST_H
                + COST_D
                + COST_S
                + COST_T
                + COST_O
                + COST_HPO
                + COST_STR
                + REGU_H
                + REGU_A
            )

            vol = (
                np.sum(thk * self.iceflow_fieldbounds["thk"]) * (self.dx**2) / 10**9
            )

            if i % params.opti_output_freq == 0:
                print(
                    " OPTI, step %5.0f , ICE_VOL: %7.2f , COST_U: %7.2f , COST_H: %7.2f , COST_D : %7.2f , COST_S : %7.2f , REGU_H : %7.2f , REGU_A : %7.2f "
                    % (
                        i,
                        vol,
                        COST_U.numpy(),
                        COST_H.numpy(),
                        COST_D.numpy(),
                        COST_S.numpy(),
                        REGU_H.numpy(),
                        REGU_A.numpy(),
                    )
                )

            self.costs.append(
                [
                    COST_U.numpy(),
                    COST_H.numpy(),
                    COST_D.numpy(),
                    COST_S.numpy(),
                    REGU_H.numpy(),
                    REGU_A.numpy(),
                ]
            )

            var_to_opti = []
            if "thk" in params.opti_control:
                var_to_opti.append(thk)
            if "usurf" in params.opti_control:
                var_to_opti.append(usurf)
            if "strflowctrl" in params.opti_control:
                var_to_opti.append(strflowctrl)

            # Compute gradient of COST w.r.t. X
            grads = tf.Variable(t.gradient(COST, var_to_opti))

            # this serve to restict the optimization of controls to the mask
            for ii in range(grads.shape[0]):
                grads[ii].assign(tf.where((self.icemaskobs > 0.5), grads[ii], 0))

            # One step of descent -> this will update input variable X
            optimizer.apply_gradients(
                zip([grads[i] for i in range(grads.shape[0])], var_to_opti)
            )

            # get back optimized variables in the pool of self.variables
            if "thk" in params.opti_control:
                self.thk = thk * self.iceflow_fieldbounds["thk"]
                self.thk = tf.where(self.thk < 0.01, 0, self.thk)
            if "strflowctrl" in params.opti_control:
                self.strflowctrl = strflowctrl * self.iceflow_fieldbounds["strflowctrl"]
            if "usurf" in params.opti_control:
                self.usurf = usurf * self.iceflow_fieldbounds["usurf"]

            self.divflux = compute_divflux(
                self.ubar, self.vbar, self.thk, self.dx, self.dx
            )

            compute_rms_std_optimization(self, i)

            self.tcomp["optimize_v1"][-1] -= time.time()
            self.tcomp["optimize_v1"][-1] *= -1

            if i % params.opti_output_freq == 0:
                update_plot_inversion(params, self, i)
                update_ncdf_optimize(params, self, i)
            # self.update_plot_profiles(params, self, i)

            # stopping criterion: stop if the cost no longer decrease
            # if i>params.opti_nbitmin:
            #     cost = [c[0] for c in costs]
            #     if np.mean(cost[-10:])>np.mean(cost[-20:-10]):
            #         break;

    # now that the ice thickness is optimized, we can fix the bed once for all!
    self.topg = self.usurf - self.thk

    # output_ncdf_optimize_final(params,self)

    plot_cost_functions(params, self, self.costs)

    np.savetxt(
        os.path.join(params.working_dir, "costs.dat"),
        np.stack(self.costs),
        fmt="%.10f",
        header="        COST_U        COST_H      COST_D       COST_S       REGU_H       REGU_A          HPO ",
    )

    np.savetxt(
        os.path.join(params.working_dir, "rms_std.dat"),
        np.stack(
            [
                self.rmsthk,
                self.stdthk,
                self.rmsvel,
                self.stdvel,
                self.rmsdiv,
                self.stddiv,
                self.rmsusurf,
                self.stdusurf,
            ],
            axis=-1,
        ),
        fmt="%.10f",
        header="        rmsthk      stdthk       rmsvel       stdvel       rmsdiv       stddiv       rmsusurf       stdusurf",
    )

    np.savetxt(
        os.path.join(params.working_dir, "strflowctrl.dat"),
        np.array(
            [
                np.mean(self.strflowctrl[self.icemaskobs > 0.5]),
                np.std(self.strflowctrl[self.icemaskobs > 0.5]),
            ]
        ),
        fmt="%.3f",
    )

    np.savetxt(
        os.path.join(params.working_dir, "volume.dat"),
        np.array([np.sum(self.thk) * self.dx * self.dx / (10**9)]),
        fmt="%.3f",
    )

    np.savetxt(
        os.path.join(params.working_dir, "tcompoptimize.dat"),
        np.array([np.sum([f for f in self.tcomp["optimize_v1"]])]),
        fmt="%.3f",
    )

    os.system(
        "echo rm "
        + os.path.join(params.working_dir, "strflowctrl.dat")
        + " >> clean.sh"
    )
    os.system(
        "echo rm " + os.path.join(params.working_dir, "rms_std.dat") + " >> clean.sh"
    )
    os.system(
        "echo rm " + os.path.join(params.working_dir, "costs.dat") + " >> clean.sh"
    )
    os.system(
        "echo rm " + os.path.join(params.working_dir, "volume.dat") + " >> clean.sh"
    )
    os.system(
        "echo rm "
        + os.path.join(params.working_dir, "tcompoptimize.dat")
        + " >> clean.sh"
    )


def update_optimize_v1(params, self):
    pass


def compute_rms_std_optimization(self, i):
    """
    compute_std_optimization
    """

    I = self.icemaskobs == 1

    if i == 0:
        self.rmsthk = []
        self.stdthk = []
        self.rmsvel = []
        self.stdvel = []
        self.rmsusurf = []
        self.stdusurf = []
        self.rmsdiv = []
        self.stddiv = []

    if hasattr(self, "profile") | hasattr(self, "thkobs"):
        ACT = ~tf.math.is_nan(self.thkobs)
        if np.sum(ACT) == 0:
            self.rmsthk.append(0)
            self.stdthk.append(0)
        else:
            self.rmsthk.append(np.nanmean(self.thk[ACT] - self.thkobs[ACT]))
            self.stdthk.append(np.nanstd(self.thk[ACT] - self.thkobs[ACT]))

    else:
        self.rmsthk.append(0)
        self.stdthk.append(0)

    if hasattr(self, "uvelsurfobs"):
        velsurf_mag = getmag(self.uvelsurf, self.vvelsurf).numpy()
        velsurfobs_mag = getmag(self.uvelsurfobs, self.vvelsurfobs).numpy()
        ACT = ~np.isnan(velsurfobs_mag)

        self.rmsvel.append(
            np.mean(velsurf_mag[(I & ACT).numpy()] - velsurfobs_mag[(I & ACT).numpy()])
        )
        self.stdvel.append(
            np.std(velsurf_mag[(I & ACT).numpy()] - velsurfobs_mag[(I & ACT).numpy()])
        )
    else:
        self.rmsvel.append(0)
        self.stdvel.append(0)

    if hasattr(self, "divfluxobs"):
        self.rmsdiv.append(np.mean(self.divfluxobs[I] - self.divflux[I]))
        self.stddiv.append(np.std(self.divfluxobs[I] - self.divflux[I]))
    else:
        self.rmsdiv.append(0)
        self.stddiv.append(0)

    if hasattr(self, "usurfobs"):
        self.rmsusurf.append(np.mean(self.usurf[I] - self.usurfobs[I]))
        self.stdusurf.append(np.std(self.usurf[I] - self.usurfobs[I]))
    else:
        self.rmsusurf.append(0)
        self.stdusurf.append(0)


def update_ncdf_optimize(params, self, it):
    """
    Initialize and write the ncdf optimze file
    """

    self.logger.info("Initialize  and write NCDF output Files")

    if "arrhenius" in params.opti_vars_to_save:
        self.arrhenius = tf.where(
            self.strflowctrl <= params.opti_thr_strflowctrl,
            self.strflowctrl,
            params.opti_thr_strflowctrl,
        )

    if "slidingco" in params.opti_vars_to_save:
        self.slidingco = tf.where(
            self.strflowctrl <= params.opti_thr_strflowctrl,
            0,
            self.strflowctrl - params.opti_thr_strflowctrl,
        )

    if "topg" in params.opti_vars_to_save:
        self.topg = self.usurf - self.thk

    if "velsurf_mag" in params.opti_vars_to_save:
        self.velsurf_mag = getmag(self.uvelsurf, self.vvelsurf)

    if "velsurfobs_mag" in params.opti_vars_to_save:
        self.velsurfobs_mag = getmag(self.uvelsurfobs, self.vvelsurfobs)

    if it == 0:
        nc = Dataset(
            os.path.join(params.working_dir, "optimize.nc"),
            "w",
            format="NETCDF4",
        )

        nc.createDimension("iterations", None)
        E = nc.createVariable("iterations", np.dtype("float32").char, ("iterations",))
        E.units = "None"
        E.long_name = "iterations"
        E.axis = "ITERATIONS"
        E[0] = it

        nc.createDimension("y", len(self.y))
        E = nc.createVariable("y", np.dtype("float32").char, ("y",))
        E.units = "m"
        E.long_name = "y"
        E.axis = "Y"
        E[:] = self.y.numpy()

        nc.createDimension("x", len(self.x))
        E = nc.createVariable("x", np.dtype("float32").char, ("x",))
        E.units = "m"
        E.long_name = "x"
        E.axis = "X"
        E[:] = self.x.numpy()

        for var in params.opti_vars_to_save:
            E = nc.createVariable(
                var, np.dtype("float32").char, ("iterations", "y", "x")
            )
            E[0, :, :] = vars(self)[var].numpy()

        nc.close()

        os.system(
            "echo rm "
            + os.path.join(params.working_dir, "optimize.nc")
            + " >> clean.sh"
        )

    else:
        nc = Dataset(
            os.path.join(params.working_dir, "optimize.nc"),
            "a",
            format="NETCDF4",
        )

        d = nc.variables["iterations"][:].shape[0]

        nc.variables["iterations"][d] = it

        for var in params.opti_vars_to_save:
            nc.variables[var][d, :, :] = vars(self)[var].numpy()

        nc.close()


def output_ncdf_optimize_final(params, self):
    """
    Write final geology after optimizing
    """

    nc = Dataset(os.path.join(params.working_dir, params.observation_file), "r")
    varori = [v for v in nc.variables]
    nc.close()

    varori.remove("x")
    varori.remove("y")
    if not "strflowctrl" in varori:
        varori.append("strflowctrl")
    if not "arrhenius" in varori:
        varori.append("arrhenius")
    if not "slidingco" in varori:
        varori.append("slidingco")
    if not "thk" in varori:
        varori.append("thk")
    if not "usurf" in varori:
        varori.append("usurf")
    if not "icemask" in varori:
        varori.append("icemask")

    self.arrhenius = tf.where(
        self.strflowctrl <= params.opti_thr_strflowctrl,
        self.strflowctrl,
        params.opti_thr_strflowctrl,
    )
    self.slidingco = tf.where(
        self.strflowctrl <= params.opti_thr_strflowctrl,
        0,
        self.strflowctrl - params.opti_thr_strflowctrl,
    )
    self.velsurf_mag = getmag(self.uvelsurf, self.vvelsurf)

    self.icemask = tf.where(
        self.thk > 1.0, tf.ones_like(self.thk), tf.zeros_like(self.thk)
    )

    nc = Dataset(
        os.path.join(params.working_dir, params.geology_optimized_file),
        "w",
        format="NETCDF4",
    )

    nc.createDimension("y", len(self.y))
    E = nc.createVariable("y", np.dtype("float32").char, ("y",))
    E.units = "m"
    E.long_name = "y"
    E.axis = "Y"
    E[:] = self.y.numpy()

    nc.createDimension("x", len(self.x))
    E = nc.createVariable("x", np.dtype("float32").char, ("x",))
    E.units = "m"
    E.long_name = "x"
    E.axis = "X"
    E[:] = self.x.numpy()

    for var in varori:
        if hasattr(self, var):
            E = nc.createVariable(var, np.dtype("float32").char, ("y", "x"))
            #                E.long_name = self.var_info[var][0]
            #                E.units     = self.var_info[var][1]
            E[:, :] = vars(self)[var].numpy()

    nc.close()

    os.system(
        "echo rm "
        + os.path.join(params.working_dir, params.geology_optimized_file)
        + " >> clean.sh"
    )


def plot_cost_functions(params, self, costs):
    costs = np.stack(costs)

    for i in range(costs.shape[1]):
        costs[:, i] -= np.min(costs[:, i])
        costs[:, i] /= np.max(costs[:, i])

    fig = plt.figure(figsize=(10, 10))
    plt.plot(costs[:, 0], "-k", label="COST U")
    plt.plot(costs[:, 1], "-r", label="COST H")
    plt.plot(costs[:, 2], "-b", label="COST D")
    plt.plot(costs[:, 3], "-g", label="COST S")
    plt.plot(costs[:, 4], "--c", label="REGU H")
    plt.plot(costs[:, 5], "--m", label="REGU A")
    plt.ylim(0, 1)
    plt.legend()

    if params.plot_live:
        plt.show()
    else:
        plt.savefig(os.path.join(params.working_dir, "convergence.png"), pad_inches=0)
        plt.close("all")

        os.system(
            "echo rm "
            + os.path.join(params.working_dir, "convergence.png")
            + " >> clean.sh"
        )


def update_plot_inversion(params, self, i):
    """
    Plot thickness, velocity, mand slidingco"""

    if hasattr(self, "uvelsurfobs"):
        velsurfobs_mag = getmag(self.uvelsurfobs, self.vvelsurfobs).numpy()
    else:
        velsurfobs_mag = np.zeros_like(self.thk.numpy())

    if hasattr(self, "usurfobs"):
        usurfobs = self.usurfobs
    else:
        usurfobs = np.zeros_like(self.thk.numpy())

    ########################################################

    fig = plt.figure(figsize=(18, 13))

    #########################################################

    ax = fig.add_subplot(2, 3, 1)
    extent = [self.x[0], self.x[-1], self.y[0], self.y[-1]]
    im1 = ax.imshow(self.thk, origin="lower", extent=extent, vmin=0, vmax=800)
    plt.colorbar(im1)

    if hasattr(self, "profile"):
        fthk = RectBivariateSpline(self.x, self.y, np.transpose(self.thk))
        for j, p in enumerate(self.profile):
            if j > 0:
                meanfitprofile = np.mean(fthk(p[:, 1], p[:, 2], grid=False) - p[:, 3])
                ax.scatter(p[:, 1], p[:, 2], c="k", s=1)
                ax.text(
                    np.mean(p[:, 1]),
                    np.mean(p[:, 2]),
                    str(int(meanfitprofile)),
                    fontsize=15,
                )

    ax.set_title(
        "THK, RMS : "
        + str(int(self.rmsthk[-1]))
        + ", STD : "
        + str(int(self.stdthk[-1])),
        size=15,
    )
    ax.axis("off")

    #########################################################

    ax = fig.add_subplot(2, 3, 2)
    velsurf_mag = getmag(self.uvelsurf, self.vvelsurf).numpy()
    im1 = ax.imshow(velsurf_mag, origin="lower", vmin=0, vmax=np.nanmax(velsurfobs_mag))
    plt.colorbar(im1, format="%.2f")
    ax.set_title(
        "MOD VEL, RMS : "
        + str(int(self.rmsvel[-1]))
        + ", STD : "
        + str(int(self.stdvel[-1])),
        size=15,
    )
    ax.axis("off")

    ########################################################

    ax = fig.add_subplot(2, 3, 3)
    im1 = ax.imshow(self.divflux, origin="lower", vmin=-15, vmax=5)
    plt.colorbar(im1, format="%.2f")
    ax.set_title(
        "MOD DIV, RMS : %5.1f , STD : %5.1f" % (self.rmsdiv[-1], self.stddiv[-1]),
        size=15,
    )
    ax.axis("off")

    #########################################################

    ax = fig.add_subplot(2, 3, 4)
    im1 = ax.imshow(self.usurf - usurfobs, origin="lower", vmin=-10, vmax=10)
    plt.colorbar(im1, format="%.2f")
    ax.set_title(
        "DELTA USURF, RMS : %5.1f , STD : %5.1f"
        % (self.rmsusurf[-1], self.stdusurf[-1]),
        size=15,
    )
    ax.axis("off")

    ########################################################

    ax = fig.add_subplot(2, 3, 5)
    im1 = ax.imshow(
        velsurfobs_mag, origin="lower", vmin=0, vmax=np.nanmax(velsurfobs_mag)
    )
    plt.colorbar(im1, format="%.2f")
    ax.set_title("OBS VEL (TARGET)", size=15)
    ax.axis("off")

    #######################################################

    ax = fig.add_subplot(2, 3, 6)
    im1 = ax.imshow(self.strflowctrl, origin="lower", vmin=0, vmax=100)
    plt.colorbar(im1, format="%.2f")
    ax.set_title("strflowctrl", size=15)
    ax.axis("off")

    #########################################################

    plt.tight_layout()

    if params.plot_live:
        plt.show()
    else:
        plt.savefig(
            os.path.join(params.working_dir, "resu-opti-" + str(i).zfill(4) + ".png"),
            pad_inches=0,
        )
        plt.close("all")

        os.system(
            "echo rm " + os.path.join(params.working_dir, "*.png") + " >> clean.sh"
        )


def update_plot_profiles(params, self, i):
    from scipy.interpolate import RectBivariateSpline

    fthk = RectBivariateSpline(self.x, self.y, np.transpose(self.thk))

    N = len(self.profile)
    N1 = int(np.sqrt(N)) + 1
    N2 = N1
    fig, axs = plt.subplots(N1, N2, figsize=(N1 * 10, N2 * 5))
    #            fig, axs = plt.subplots(N,1,figsize=(10,N*4))
    for j, p in enumerate(self.profile):
        if j > 0:
            jj = j // N1
            ii = j % N1
            axs[ii, jj].set_title(" PROFILE N° : " + str(j))
            axs[ii, jj].plot(p[:, 0], p[:, 3], "-k")
            axs[ii, jj].plot(p[:, 0], fthk(p[:, 1], p[:, 2], grid=False), "-b")
            axs[ii, jj].axis("equal")
    plt.tight_layout()

    if params.plot_live:
        plt.show()
    else:
        plt.savefig(
            os.path.join(params.working_dir, "S1-pro-" + str(i).zfill(4) + ".png"),
            pad_inches=0,
        )
        plt.close("all")

        os.system(
            "echo rm " + os.path.join(params.working_dir, "*.png") + " >> clean.sh"
        )


def compute_flow_direction_for_anisotropic_smoothing(self):
    """
    compute_flow_direction_for_anisotropic_smoothing
    """

    uvelsurfobs = tf.where(tf.math.is_nan(self.uvelsurfobs), 0.0, self.uvelsurfobs)
    vvelsurfobs = tf.where(tf.math.is_nan(self.vvelsurfobs), 0.0, self.vvelsurfobs)

    self.flowdirx = (
        uvelsurfobs[1:, 1:]
        + uvelsurfobs[:-1, 1:]
        + uvelsurfobs[1:, :-1]
        + uvelsurfobs[:-1, :-1]
    ) / 4.0
    self.flowdiry = (
        vvelsurfobs[1:, 1:]
        + vvelsurfobs[:-1, 1:]
        + vvelsurfobs[1:, :-1]
        + vvelsurfobs[:-1, :-1]
    ) / 4.0

    from scipy.ndimage import gaussian_filter

    self.flowdirx = gaussian_filter(self.flowdirx, 3, mode="constant")
    self.flowdiry = gaussian_filter(self.flowdiry, 3, mode="constant")

    # Same as gaussian filter above but for tensorflow is (NOT TESTED)
    # import tensorflow_addons as tfa
    # self.flowdirx = ( tfa.image.gaussian_filter2d( self.flowdirx , sigma=3, filter_shape=100, padding="CONSTANT") )

    self.flowdirx /= getmag(self.flowdirx, self.flowdiry)
    self.flowdiry /= getmag(self.flowdirx, self.flowdiry)

    self.flowdirx = tf.where(tf.math.is_nan(self.flowdirx), 0.0, self.flowdirx)
    self.flowdiry = tf.where(tf.math.is_nan(self.flowdiry), 0.0, self.flowdiry)

    # this is to plot the observed flow directions
    # fig, axs = plt.subplots(1, 1, figsize=(8,16))
    # plt.quiver(self.flowdirx,self.flowdiry)
    # axs.axis("equal")