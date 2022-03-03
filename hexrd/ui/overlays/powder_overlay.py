import numpy as np

from hexrd import constants
from hexrd import unitcell

from hexrd.transforms import xfcapi

from hexrd.ui.constants import OverlayType, ViewType
from hexrd.ui.overlays.overlay import Overlay


class PowderOverlay(Overlay):

    type = OverlayType.powder
    hkl_data_key = 'rings'

    def __init__(self, material_name, tvec=None, eta_steps=360,
                 eta_period=None, **overlay_kwargs):
        super().__init__(material_name, **overlay_kwargs)

        if tvec is None:
            tvec = constants.zeros_3.copy()

        if eta_period is None:
            eta_period = np.r_[-180., 180.]

        self.tvec = tvec
        self.eta_steps = eta_steps
        self.eta_period = eta_period

    @property
    def child_attributes_to_save(self):
        # These names must be identical here, as attributes, and as
        # arguments to the __init__ method.
        return [
            'tvec',
            'eta_steps',
            'eta_period',
        ]

    @property
    def has_widths(self):
        return self.material.planeData.tThWidth is not None

    @property
    def tvec(self):
        return self._tvec

    @tvec.setter
    def tvec(self, x):
        x = np.asarray(x, float).flatten()
        assert len(x) == 3, "tvec input must have exactly 3 elements"
        self._tvec = x

    @property
    def eta_steps(self):
        return self._eta_steps

    @eta_steps.setter
    def eta_steps(self, x):
        assert isinstance(x, int), 'input must be an int'
        self._eta_steps = x

    @property
    def delta_eta(self):
        return 360 / self.eta_steps

    @property
    def eta_period(self):
        return self._eta_period

    @eta_period.setter
    def eta_period(self, x):
        x = np.asarray(x, float).flatten()
        assert len(x) == 2, "eta period must be a 2-element sequence"
        if xfcapi.angularDifference(x[0], x[1], units='degrees') > 1e-4:
            raise RuntimeError("period specification is not 360 degrees")
        self._eta_period = x

    @property
    def all_refinements(self):
        # This doesn't take into account crystal symmetry
        if not hasattr(self, '_all_refinements'):
            self._all_refinements = self.default_refinements
        return self._all_refinements

    @all_refinements.setter
    def all_refinements(self, v):
        if len(v) != 6:
            raise Exception(f'{len(v)=} must be 6')
        self._all_refinements = np.asarray(v)

    @property
    def refinements(self):
        # Only return the required indices
        return self.all_refinements[self.refinement_indices]

    @refinements.setter
    def refinements(self, v):
        if len(v) == 6:
            self.all_refinements = v
        elif len(v) == len(self.refinement_indices):
            self.all_refinements[self.refinement_indices] = v
        else:
            msg = f'{v=} must be length 6 or {len(self.refinement_indices)=}'
            raise Exception(msg)

    @property
    def refinement_indices(self):
        if self.material is None:
            return np.asarray(range(6))
        ltype = self.material.unitcell.latticeType
        return np.asarray(unitcell._rqpDict[ltype][0])

    @property
    def all_refinement_labels(self):
        return np.asarray(['a', 'b', 'c', 'α', 'β', 'γ'])

    @property
    def refinement_labels(self):
        if self.material is None:
            return self.all_refinement_labels

        return self.all_refinement_labels[self.refinement_indices]

    @property
    def default_refinements(self):
        return np.asarray([False] * 6)

    def generate_overlay(self):
        instr = self.instrument
        plane_data = self.plane_data
        display_mode = self.display_mode

        tths = plane_data.getTTh()
        hkls = plane_data.getHKLs()
        etas = np.radians(
            np.linspace(
                -180., 180., num=self.eta_steps + 1
            )
        )

        if tths.size == 0:
            # No overlays
            return {}

        if plane_data.tThWidth is not None:
            # Need to get width data as well
            indices, ranges = plane_data.getMergedRanges()
            r_lower = [r[0] for r in ranges]
            r_upper = [r[1] for r in ranges]

        point_groups = {}
        for det_key, panel in instr.detectors.items():
            keys = ['rings', 'rbnds', 'rbnd_indices', 'hkls']
            point_groups[det_key] = {key: [] for key in keys}
            ring_pts, skipped_tth = self.generate_ring_points(
                instr, tths, etas, panel, display_mode)

            det_hkls = [x for i, x in enumerate(hkls) if i not in skipped_tth]

            point_groups[det_key]['rings'] = ring_pts
            point_groups[det_key]['hkls'] = det_hkls

            if plane_data.tThWidth is not None:
                # Generate the ranges too
                lower_pts, _ = self.generate_ring_points(
                    instr, r_lower, etas, panel, display_mode
                )
                upper_pts, _ = self.generate_ring_points(
                    instr, r_upper, etas, panel, display_mode
                )
                for lpts, upts in zip(lower_pts, upper_pts):
                    point_groups[det_key]['rbnds'] += [lpts, upts]
                for ind in indices:
                    point_groups[det_key]['rbnd_indices'] += [ind, ind]

        return point_groups

    def generate_ring_points(self, instr, tths, etas, panel, display_mode):
        delta_eta_nom = np.degrees(np.median(np.diff(etas)))
        ring_pts = []
        skipped_tth = []
        for i, tth in enumerate(tths):
            # construct ideal angular coords
            ang_crds = np.vstack([np.tile(tth, len(etas)), etas]).T

            # Convert nominal powder angle coords to cartesian
            # !!! Tricky business; here we must consider _both_ the SAMPLE
            #     CS origin and anything specified for the XRD COM for the
            #     overlay.  This is so they get properly mapped back to the
            #     the proper cartesian coords.
            xys_full = panel.angles_to_cart(
                ang_crds,
                tvec_s=instr.tvec,
                tvec_c=self.tvec
            )

            # skip if ring not on panel
            if len(xys_full) == 0:
                skipped_tth.append(i)
                continue

            # clip to detector panel
            xys, on_panel = panel.clip_to_panel(
                xys_full, buffer_edges=False
            )

            if display_mode == ViewType.polar:
                # Apply offset correction
                # !!! In polar view, the nominal angles refer to the SAMPLE
                #     CS origin, so we omit the addition of any offset to the
                #     diffraction COM in the sameple frame!
                ang_crds, _ = panel.cart_to_angles(
                    xys,
                    tvec_s=instr.tvec
                )
                if len(ang_crds) == 0:
                    skipped_tth.append(i)
                    continue

                # Swap columns, convert to degrees
                ang_crds[:, [0, 1]] = np.degrees(ang_crds[:, [1, 0]])

                # fix eta period
                ang_crds[:, 0] = xfcapi.mapAngle(
                    ang_crds[:, 0], self.eta_period, units='degrees'
                )

                # sort points for monotonic eta
                eidx = np.argsort(ang_crds[:, 0])
                ang_crds = ang_crds[eidx, :]

                # branch cut
                # FIXME: still is not quite right
                delta_eta_est = np.median(np.diff(ang_crds[:, 0]))
                cut_on_panel = bool(
                    xfcapi.angularDifference(
                        np.min(ang_crds[:, 0]),
                        np.max(ang_crds[:, 0]),
                        units='degrees'
                    ) < 2*delta_eta_est
                )
                if cut_on_panel and len(ang_crds) > 2:
                    split_idx = np.argmax(
                        np.abs(np.diff(ang_crds[:, 0]) - delta_eta_est)
                    ) + 1

                    ang_crds = np.vstack(
                        [ang_crds[:split_idx, :],
                         nans_row,
                         ang_crds[split_idx:, :]]
                    )

                # append to list with nan padding
                ring_pts.append(np.vstack([ang_crds, nans_row]))

            elif display_mode in [ViewType.raw, ViewType.cartesian]:

                if display_mode == ViewType.raw:
                    # !!! distortion
                    if panel.distortion is not None:
                        xys = panel.distortion.apply_inverse(xys)

                    # Convert to pixel coordinates
                    # ??? keep in pixels?
                    xys = panel.cartToPixel(xys)

                diff_tol = np.radians(self.delta_eta) + 1e-4
                ring_breaks = np.where(
                    np.abs(np.diff(etas[on_panel])) > diff_tol
                )[0] + 1
                n_segments = len(ring_breaks) + 1

                if n_segments == 1:
                    ring_pts.append(np.vstack([xys, nans_row]))
                else:
                    src_len = sum(on_panel)
                    dst_len = src_len + len(ring_breaks)
                    nxys = np.nan*np.ones((dst_len, 2))
                    ii = 0
                    for i in range(n_segments - 1):
                        jj = ring_breaks[i]
                        nxys[ii + i:jj + i, :] = xys[ii:jj, :]
                        ii = jj
                    i = n_segments - 1
                    nxys[ii + i:, :] = xys[ii:, :]
                    ring_pts.append(np.vstack([nxys, nans_row]))

        return ring_pts, skipped_tth

    @property
    def default_style(self):
        return {
            'data': {
                'c': '#00ffff',  # Cyan
                'ls': 'solid',
                'lw': 1.0
            },
            'ranges': {
                'c': '#00ff00',  # Green
                'ls': 'dotted',
                'lw': 1.0
            }
        }

    @property
    def default_highlight_style(self):
        return {
            'data': {
                'c': '#ff00ff',  # Magenta
                'ls': 'solid',
                'lw': 3.0
            },
            'ranges': {
                'c': '#ff00ff',  # Magenta
                'ls': 'dotted',
                'lw': 3.0
            }
        }


# Constants
nans_row = np.nan * np.ones((1, 2))