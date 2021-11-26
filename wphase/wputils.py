# -*- coding: utf-8 -*-
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import os
import logging
import numpy as np
from collections import defaultdict
from traceback import format_exc

# to avoid: Exception _tkinter.TclError
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
try:
    from obspy.imaging.beachball import (beachball as plot_beachball,
                                         aux_plane, mt2plane, MomentTensor)
except ImportError:
    from obspy.imaging.beachball import (Beachball as plot_beachball,
                                         AuxPlane as aux_plane,
                                         MT2Plane as mt2plane,
                                         MomentTensor)


from wphase.psi import seismoutils
from wphase.plotting import plot_grid_search, plot_station_coverage, plot_preliminary_fit, plot_waveforms
from wphase.psi.model import OL1, OL2, OL3
from wphase import settings

logger = logging.getLogger(__name__)


if settings.PROFILE_WPHASE:
    try:
        # If we can import pyinstrument imported, then profile
        from pyinstrument import Profiler
        class WPInvProfiler(object):
            def __init__(self, wphase_output, working_dir):
                self.wphase_output = wphase_output
                self.working_dir = working_dir

            def __enter__(self):
                self.profiler = Profiler() # or Profiler(use_signal=False), see below
                self.profiler.start()

            def __exit__(self, exc_type, esc_value, traceback):
                self.profiler.stop()
                self.wphase_output[settings.WPINV_PROFILE_OUTPUT_KEY] = \
                    self.profiler.output_html()
                if self.working_dir is not None:
                    with open(os.path.join(self.working_dir, 'timings.html'), 'w') as timings_file:
                        timings_file.write(self.profiler.output_html())

    except Exception:
        import cProfile, pstats, io
        class WPInvProfiler(object):
            def __init__(self, wphase_output, *args, **kwargs):
                self.wphase_output = wphase_output
                self.sort_by = 'cumulative'#'tottime'

            def __enter__(self):
                self.profiler = cProfile.Profile()
                self.profiler.enable()

            def __exit__(self, exc_type, esc_value, traceback):
                self.profiler.disable()
                s = io.StringIO()
                ps = pstats.Stats(self.profiler, stream=s).sort_stats(self.sort_by)
                ps.print_stats()
                self.wphase_output[settings.WPINV_PROFILE_OUTPUT_KEY] = {
                    'css':'',
                    'js':'',
                    'body':'<pre>{}</pre>'.format(s.getvalue())}
else:
    class WPInvProfiler(object):
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): pass
        def __exit__(self, exc_type, esc_value, traceback): pass





class OutputDict(defaultdict):
    """
    A dict with an additional method for adding warnings and which converts any
    numpy array in itself or the items it holds (recursively) to a python list.
    """

    def __init__(self):
        super(OutputDict, self).__init__(OutputDict)

    def __setitem__(self, key, value):
        def _recurse(d):
            if isinstance(d, dict) and not isinstance(d, OutputDict):
                res = OutputDict()
                for k, v in d.items():
                    super(OutputDict, res).__setitem__(k, _recurse(v))
                return res
            elif isinstance(d, np.ndarray):
                return list(d)
            return d

        return super(OutputDict, self).__setitem__(key, _recurse(value))

    def add_warning(self, msg):
        """
        Add a the warning message given by *msg*.

        Warnings are accumulated in a list under the key
        :py:data:`wphase.settings.WPHASE_WARNINGS_KEY`.
        """

        if settings.WPHASE_WARNINGS_KEY not in self:
            self[settings.WPHASE_WARNINGS_KEY] = []
        self[settings.WPHASE_WARNINGS_KEY].append(msg)

    def as_dict(self, item=None):
        if item is None:
            return {k: None if v is None else self.as_dict(v) for k, v in self.items()}
        if isinstance(item, OutputDict):
            return item.as_dict()
        return item





def wpinv_for_eatws(M, cenloc):
    """
    Convert Roberto's wphase output to antelope's moment tensor format.
    Plus calculate a few values based on the moment tensor.

    :param M: Moment tensor
    :param cenloc: The centroid location, (cenlat,cenlon,cendep)
    :return: antelope's moment tensor info in a dictionary.
    """

    results = {}
    results['tmpp'] = M[2]
    results['tmrp'] = M[4]
    results['tmrr'] = M[0]
    results['tmrt'] = M[3]
    results['tmtp'] = M[5]
    results['tmtt'] = M[1]

    try:
        DC, CLVD = decomposeMT(M)
        results['dc'] = DC
        results['clvd'] = CLVD
    except Exception:
        import traceback
        logger.warning("Error computing DC/CLVD decomposition: %s",
                       traceback.format_exc())

    # from roberto's code
    M2 = M*M
    m0 = np.sqrt(0.5*(M2[0]+M2[1]+M2[2])+M2[3]+M2[4]+M2[5])
    mag = 2./3.*(np.log10(m0)-9.10)

    results['scm'] = m0
    results['drmag'] = mag
    results['drmagt'] = 'Mww'

    results['drlat'] = cenloc[0]
    results['drlon'] = cenloc[1]
    results['drdepth'] = cenloc[2]

    moment_tensor = MomentTensor(M, 0)
    nodalplane = mt2plane(moment_tensor)
    results['str1'] = nodalplane.strike
    results['dip1'] = nodalplane.dip
    results['rake1'] = nodalplane.rake

    np2 = aux_plane(
        nodalplane.strike,
        nodalplane.dip,
        nodalplane.rake)
    results['str2'] = np2[0]
    results['dip2'] = np2[1]
    results['rake2'] = np2[2]

    results['auth'] = settings.GA_AUTHORITY

    return results





def post_process_wpinv(
    res,
    wphase_output,
    WPOL,
    working_dir,
    eqinfo,
    metadata,
    make_maps=True,
    make_plots=True):

    prelim = res.preliminary_calc_details

    if prelim:
        fname = os.path.join(working_dir, settings.WPHASE_PRELIM_FIT_PREFIX) + '.png'
        plot_preliminary_fit(eqinfo, filename=fname, **prelim)
    else:
        logger.warning("Could not find preliminary calculation details in result.")

    M_OL2 = None
    WPOL = 1
    traces = res.used_traces

    if WPOL >= 2:
        wphase_output['QualityParams']['azimuthal_gap'] = seismoutils.AzimuthalGap(
                metadata,
                traces,
                (eqinfo['lat'], eqinfo['lon']))[0]
        wphase_output['QualityParams']['number_of_stations'] = len(set(
            trid.split('.')[1] for trid in traces))
        wphase_output['QualityParams']['number_of_channels'] = len(traces)


    # extract the results to local variables
    if isinstance(res, OL2):
        WPOL = 2
        M = res.moment_tensor
        obs = res.observed_displacements
        syn = res.synthetic_displacements
        traces = res.trace_lengths
    else:
        traces = list(res.preliminary_calc_details['trids'])

    if isinstance(res, OL3):
        WPOL = 3
        M_OL2 = wphase_output['OL2'].pop('M')
        cenloc = res.centroid

        results = wpinv_for_eatws(M, cenloc)
        wphase_output['MomentTensor'] = results

        # Only 3 has cenloc...
        wphase_output['Centroid'] = {}
        wphase_output['Centroid']['depth'] = round(cenloc[2],1)
        wphase_output['Centroid']['latitude'] = round(cenloc[0],3)
        wphase_output['Centroid']['longitude'] = round(cenloc[1],3)

        if make_plots:
            try:
                # Display the beachball for OL2
                beachBallPrefix = os.path.join(
                    working_dir,
                    settings.WPHASE_BEACHBALL_PREFIX)
                plot_beachball(M_OL2, width=400,
                    outfile = beachBallPrefix + "_OL2.png", format='png')
                plt.close('all') # obspy doesn't clean up after itself...
            except Exception:
                wphase_output.add_warning("Failed to create beachball for OL2.")

    if 'OL2' in wphase_output:
        wphase_output['OL2'].pop('M', None)

    if make_maps:
        try:
            # Make a plot of the station distribution
            hyplat =  eqinfo['lat']
            hyplon =  eqinfo['lon']
            lats = [metadata[trid]['latitude'] for trid in traces]
            lons = [metadata[trid]['longitude'] for trid in traces]
            stationDistPrefix = os.path.join(
                working_dir,
                settings.WPHASE_STATION_DISTRIBUTION_PREFIX)
            plot_station_coverage(
                (hyplat,hyplon),
                lats,
                lons,
                mt=M,
                filename=stationDistPrefix + '.png')
        except Exception:
            wphase_output.add_warning("Failed to create station distribution plot. {}".format(format_exc()))


    if make_plots:
        try:
            # Display the beachball for the output level achieved
            beachBallPrefix = os.path.join(working_dir, "{}_OL{}".format(
                settings.WPHASE_BEACHBALL_PREFIX, WPOL))
            plot_beachball(M, width=400,
                outfile = beachBallPrefix + ".png", format='png')
            plt.close('all') # obspy doesn't clean up after itself...
        except Exception:
            wphase_output.add_warning("Failed to create beachball for OL{}. {}".format(
                WPOL, format_exc()))

    if WPOL >= 2 and make_plots and len(traces) > 0:
        # Secondly the wphase traces plot, syn Vs obs
        plot_waveforms(
            working_dir,
            settings.WPHASE_RESULTS_TRACES_PREFIX,
            syn,
            obs,
            res.trace_lengths)

    elif make_plots:
        wphase_output.add_warning('Could not create wphase results plot. OL=%d, len(traces)=%d' % (WPOL, len(traces)))

    if WPOL==3:
        if make_maps:
            # draw the grid search plot
            coords = np.asarray(res.grid_search_candidates)
            misfits = np.array([x[1] for x in res.grid_search_results])
            N_grid = len(misfits)
            lats, lons, depths = coords.T
            depths_unique  = sorted(set(depths))
            N_depths = len(depths_unique)
            misfits_depth_mat = np.zeros((int(N_grid/N_depths),N_depths))
            latlon_depth_mat = np.zeros((int(N_grid/N_depths),2,N_depths))
            ##We will sum the misfits over the depths
            for i_col,depth in enumerate(depths_unique):
                i_depth = np.where(depths == depth)
                misfits_depth_mat[:,i_col] = misfits[i_depth]
                latlon_depth_mat[:,:,i_col] = coords[i_depth,:2]

            ##This should be the same for all depths
            latlon_depth_grid =  latlon_depth_mat[:,:,0]
            #Suming all the depths
            misfits_depth_mat =  misfits_depth_mat.sum(axis=1)
            scaled_field = misfits_depth_mat/misfits_depth_mat.min()

            gridSearchPrefix = os.path.join(working_dir, settings.WPHASE_GRID_SEARCH_PREFIX)
            plot_grid_search(
                (eqinfo['lon'], eqinfo['lat']),
                (cenloc[1], cenloc[0]),
                latlon_depth_grid,
                scaled_field,
                s=100./scaled_field**2,
                c=scaled_field,
                zorder=999,
                filename=gridSearchPrefix
            )


def decomposeMT(M):
    """Given a deviatoric (i.e. trace-free) moment tensor (specified as a
    6-element list in the CMT convention as usual), compute the percentage
    double-couple and compensated linear vector dipole components.

    Written following this paper:
    Vavryčuk, V. Moment tensor decompositions revisited. J Seismol 19, 231–252
    (2015). https://doi.org/10.1007/s10950-014-9463-y

    :returns: A tuple ``(DC, CLVD)`` of relative scale factors between 0 and 1.
    """
    mt = MomentTensor(M, 0)
    eigs, _ = np.linalg.eig(mt.mt)
    M1, M2, M3 = np.sort(eigs)[::-1] # M1 >= M2 >= M3

    # Since we're working with deviatoric moment tensors, we are assuming
    # M_ISO=0 and we don't have to worry about the destinction between the
    # Silver&Jordan and Knopoff&Randall decompositions.
    M_CLVD = (2./3.)*(M1 + M3 - 2*M2)
    M_DC = (1./2.)*(M1 - M3 - abs(M1 + M3 - 2*M2))
    M = abs(M_CLVD) + abs(M_DC)
    return abs(M_DC)/M, abs(M_CLVD)/M
