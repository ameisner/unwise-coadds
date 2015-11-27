#! /usr/bin/env python

import matplotlib
if __name__ == '__main__':
    matplotlib.use('Agg')
import numpy as np
import pylab as plt
import os
import sys
import tempfile
import datetime
import gc
from scipy.ndimage.morphology import binary_dilation
from scipy.ndimage.measurements import label, center_of_mass
from zp_lookup import ZPLookUp
import random

import fitsio

arrayblock = 20000

if __name__ == '__main__':
    arr = os.environ.get('PBS_ARRAYID')
    d = os.environ.get('PBS_O_WORKDIR')
    if arr is not None and d is not None:
        os.chdir(d)
        sys.path.append(os.getcwd())

from astrometry.util.file import *
from astrometry.util.fits import *
from astrometry.util.multiproc import *
from astrometry.util.plotutils import *
from astrometry.util.miscutils import *
from astrometry.util.util import *
from astrometry.util.resample import *
from astrometry.util.run_command import *
from astrometry.util.starutil_numpy import *
from astrometry.util.ttime import *
from astrometry.libkd.spherematch import *

import logging
lvl = logging.INFO
logging.basicConfig(level=lvl, format='%(message)s', stream=sys.stdout)

#median_f = np.median
median_f = flat_median_f

# GLOBALS:
# Location of WISE Level 1b inputs
unwise_symlink_dir = os.environ.get('UNWISE_SYMLINK_DIR')
if unwise_symlink_dir is None:
    unwise_symlink_dir = '/scratch1/scratchdirs/ameisner/code/unwise-coadds'

wisedir = os.path.join(unwise_symlink_dir, 'wise-frames')

wisedirs = [wisedir, os.path.join(unwise_symlink_dir, 'neowiser-frames'), 'merge_p1bm_frm']

mask_gz = True
unc_gz = True
int_gz = None # should get assigned in main
use_zp_meta = None # should get assigned in main

def tile_to_radec(tileid):
    assert(len(tileid) == 8)
    ra = int(tileid[:4], 10) / 10.
    sign = -1 if tileid[4] == 'm' else 1
    dec = sign * int(tileid[5:], 10) / 10.
    return ra,dec

def get_l1b_file(basedir, scanid, frame, band):
    scangrp = scanid[-2:]
    fname = os.path.join(basedir, scangrp, scanid, '%03i' % frame, 
                        '%s%03i-w%i-int-1b.fits' % (scanid, frame, band))
    if int_gz:
        fname += '.gz'
    return fname

def int_from_scan_frame(scan_id, frame_num):
    val_str = scan_id[0:5] + str(frame_num).zfill(3)
    val = int(val_str)
    return val

# from tractor.basics.NanoMaggies
def zeropointToScale(zp):
    '''
    Converts a traditional magnitude zeropoint to a scale factor
    by which nanomaggies should be multiplied to produce image
    counts.
    '''
    return 10.**((zp - 22.5)/2.5)

class Duck():
    pass

def get_coadd_tile_wcs(ra, dec, W=2048, H=2048, pixscale=2.75):
    '''
    Returns a Tan WCS object at the given RA,Dec center, axis aligned, with the
    given pixel W,H and pixel scale in arcsec/pixel.
    '''
    cowcs = Tan(ra, dec, (W+1)/2., (H+1)/2.,
                -pixscale/3600., 0., 0., pixscale/3600., W, H)
    return cowcs

def walk_wcs_boundary(wcs, step=1024, margin=0):
    '''
    Walk the image boundary counter-clockwise.

    Returns rr,dd -- RA,Dec numpy arrays.
    '''
    W = wcs.get_width()
    H = wcs.get_height()
    xlo = 1
    xhi = W
    ylo = 1
    yhi = H
    if margin:
        xlo -= margin
        ylo -= margin
        xhi += margin
        yhi += margin
    
    xx,yy = [],[]
    xwalk = np.linspace(xlo, xhi, int(np.ceil((1+xhi-xlo)/float(step)))+1)
    ywalk = np.linspace(ylo, yhi, int(np.ceil((1+yhi-ylo)/float(step)))+1)
    # bottom edge
    x = xwalk[:-1]
    y = ylo
    xx.append(x)
    yy.append(np.zeros_like(x) + y)
    # right edge
    x = xhi
    y = ywalk[:-1]
    xx.append(np.zeros_like(y) + x)
    yy.append(y)
    # top edge
    x = list(reversed(xwalk))[:-1]
    y = yhi
    xx.append(x)
    yy.append(np.zeros_like(x) + y)
    # left edge
    x = xlo
    y = list(reversed(ywalk))[:-1]
    # (note, NOT closed)
    xx.append(np.zeros_like(y) + x)
    yy.append(y)
    #
    rr,dd = wcs.pixelxy2radec(np.hstack(xx), np.hstack(yy))
    return rr,dd

def get_wcs_radec_bounds(wcs):
    rr,dd = walk_wcs_boundary(wcs)
    r0,r1 = rr.min(), rr.max()
    d0,d1 = dd.min(), dd.max()
    return r0,r1,d0,d1

def get_atlas_tiles(r0,r1,d0,d1, W=2048, H=2048, pixscale=2.75):
    '''
    Select Atlas Image tiles touching a desired RA,Dec box.

    pixscale in arcsec/pixel
    '''
    # Read Atlas Image table
    fn = os.path.join(wisedir, 'wise_allsky_4band_p3as_cdd.fits')
    print 'Reading', fn
    T = fits_table(fn, columns=['coadd_id', 'ra', 'dec'])
    T.row = np.arange(len(T))
    print 'Read', len(T), 'Atlas tiles'

    margin = (max(W,H) / 2.) * (pixscale / 3600.)

    T.cut(in_radec_box(T.ra, T.dec, r0,r1,d0,d1, margin))
    print 'Cut to', len(T), 'Atlas tiles near RA,Dec box'

    T.coadd_id = np.array([c.replace('_ab41','') for c in T.coadd_id])

    # Some of them don't *actually* touch our RA,Dec box...
    print 'Checking tile RA,Dec bounds...'
    keep = []
    for i in range(len(T)):
        wcs = get_coadd_tile_wcs(T.ra[i], T.dec[i], W, H, pixscale)
        R0,R1,D0,D1 = get_wcs_radec_bounds(wcs)
        # FIXME RA wrap
        if R1 < r0 or R0 > r1 or D1 < d0 or D0 > d1:
            print 'Coadd tile', T.coadd_id[i], 'is outside RA,Dec box'
            continue
        keep.append(i)
    T.cut(np.array(keep))
    print 'Cut to', len(T), 'tiles'
    # sort
    T.cut(np.argsort(T.coadd_id))
    return T

def in_radec_box(ra,dec, r0,r1,d0,d1, margin):
    assert(r0 <= r1)
    assert(d0 <= d1)
    assert(margin >= 0.)
    if r0 == 0. and r1 == 360.:
        # Just cut on Dec.
        return ((dec + margin >= d0) * (dec - margin <= d1))
        
    cosdec = np.cos(np.deg2rad(max(abs(d0),abs(d1))))
    print 'cosdec:', cosdec
    # wrap-around... time to switch to unit-sphere instead?
    # Still issues near the Dec poles (if margin/cosdec -> 360)
    if (r0 - margin/cosdec < 0) or (r1 + margin/cosdec > 360):
        # python mod: result has same sign as second arg
        rlowrap = (r0 - margin/cosdec) % 360.0
        rhiwrap = (r1 + margin/cosdec) % 360.0
        if (r0 - margin/cosdec < 0):
            raA = rlowrap
            raB = 360.
            raC = 0.
            raD = rhiwrap
        else:
            raA = rhiwrap
            raB = 360.0
            raC = 0.
            raD = rlowrap
        print 'RA wrap-around:', r0,r1, '+ margin', margin, '->', rlowrap, rhiwrap
        print 'Looking at ranges (%.2f, %.2f) and (%.2f, %.2f)' % (raA,raB,raC,raD)
        assert(raA <= raB)
        assert(raC <= raD)
        return (np.logical_or((ra >= raA) * (ra <= raB),
                              (ra >= raC) * (ra <= raD)) *
                (dec + margin >= d0) *
                (dec - margin <= d1))
    else:
        return ((ra + margin/cosdec >= r0) *
                (ra - margin/cosdec <= r1) *
                (dec + margin >= d0) *
                (dec - margin <= d1))

def get_wise_frames(r0,r1,d0,d1, margin=2.):
    '''
    Returns WISE frames touching the given RA,Dec box plus margin.
    '''
    # Read WISE frame metadata
    WISE = fits_table(os.path.join(wisedir, 'WISE-index-L1b.fits'))
    print 'Read', len(WISE), 'WISE L1b frames'
    WISE.row = np.arange(len(WISE))

    # Coarse cut on RA,Dec box.
    WISE.cut(in_radec_box(WISE.ra, WISE.dec, r0,r1,d0,d1, margin))
    print 'Cut to', len(WISE), 'WISE frames near RA,Dec box'

    # Join to WISE Single-Frame Metadata Tables
    WISE.planets = np.zeros(len(WISE), np.int16) - 1
    WISE.qual_frame = np.zeros(len(WISE), np.int16) - 1
    WISE.moon_masked = np.zeros(len(WISE), bool)
    WISE.dtanneal = np.zeros(len(WISE), np.float32)

    # pixel distribution stats (used for moon masking)
    WISE.intmedian = np.zeros(len(WISE), np.float32)
    WISE.intstddev = np.zeros(len(WISE), np.float32)
    WISE.intmed16p = np.zeros(len(WISE), np.float32)
    WISE.matched = np.zeros(len(WISE), bool)

    # 4-band, 3-band, or 2-band phase
    WISE.phase = np.zeros(len(WISE), np.uint8)
    
    for nbands,name in [(4,'4band'), (3,'3band'), (2,'2band'), (2,'neowiser'),
                        (2, 'neowiser2'),
                        ]:
        metadir = os.environ.get('UNWISE_META_DIR')
        if metadir is None:
            metadir = wisedir
        fn = os.path.join(metadir, 'WISE-l1b-metadata-%s.fits' % name)
        print 'Reading', fn
        bb = [1,2,3,4][:nbands]
        cols = (['ra', 'dec', 'scan_id', 'frame_num',
                 'qual_frame', 'planets', 'moon_masked', ] +
                ['w%iintmed16ptile' % b for b in bb] +
                ['w%iintmedian' % b for b in bb] +
                ['w%iintstddev' % b for b in bb])
        if nbands > 2:
            cols.append('dtanneal')
        T = fits_table(fn, columns=cols)
        print 'Read', len(T), 'from', fn
        # Cut with extra large margins
        T.cut(in_radec_box(T.ra, T.dec, r0,r1,d0,d1, 2.*margin))
        print 'Cut to', len(T), 'near RA,Dec box'
        if len(T) == 0:
            continue

        if not 'dtanneal' in T.get_columns():
            T.dtanneal = np.zeros(len(T), np.float64) + 1000000.
            
        I,J,d = match_radec(WISE.ra, WISE.dec, T.ra, T.dec, 60./3600.)
        print 'Matched', len(I)

        print 'WISE-index-L1b scan_id:', WISE.scan_id.dtype, 'frame_num:', WISE.frame_num.dtype
        print 'WISE-metadata scan_id:', T.scan_id.dtype, 'frame_num:', T.frame_num.dtype

        K = np.flatnonzero((WISE.scan_id  [I] == T.scan_id  [J]) *
                           (WISE.frame_num[I] == T.frame_num[J]))
        I = I[K]
        J = J[K]
        print 'Cut to', len(I), 'matching scan/frame'

        for band in bb:
            K = (WISE.band[I] == band)
            print 'Band', band, ':', sum(K)
            if sum(K) == 0:
                continue
            II = I[K]
            JJ = J[K]
            WISE.qual_frame [II] = T.qual_frame [JJ].astype(WISE.qual_frame.dtype)
            moon = T.moon_masked[JJ]
            WISE.moon_masked[II] = np.array([m[band-1] == '1' for m in moon]
                                            ).astype(WISE.moon_masked.dtype)
            WISE.dtanneal [II] = T.dtanneal[JJ].astype(WISE.dtanneal.dtype)
            WISE.intmedian[II] = T.get('w%iintmedian' % band)[JJ].astype(np.float32)
            WISE.intstddev[II] = T.get('w%iintstddev' % band)[JJ].astype(np.float32)
            WISE.intmed16p[II] = T.get('w%iintmed16ptile' % band)[JJ].astype(np.float32)
            WISE.matched[II] = True
            WISE.phase[II] = nbands
            WISE.planets[II] = T.planets[JJ]

    print np.sum(WISE.matched), 'of', len(WISE), 'matched to metadata tables'
    print np.unique(WISE.planets)
    assert(np.sum(WISE.matched) == len(WISE))
    WISE.delete_column('matched')
    # Reorder by scan, frame, band
    WISE.cut(np.lexsort((WISE.band, WISE.frame_num, WISE.scan_id)))
    return WISE

def check_one_md5(wise):
    intfn = get_l1b_file(wisedir, wise.scan_id, wise.frame_num, wise.band)
    uncfn = intfn.replace('-int-', '-unc-')
    if unc_gz and (not int_gz):
        uncfn = uncfn + '.gz'
    maskfn = intfn.replace('-int-', '-msk-')
    if mask_gz and (not int_gz):
        maskfn = maskfn + '.gz'
    instr = ''
    ok = True
    for fn in [intfn,uncfn,maskfn]:
        if not os.path.exists(fn):
            print >>sys.stderr, '%s: DOES NOT EXIST' % fn
            ok = False
            continue
        mdfn = fn + '.md5'
        if not os.path.exists(mdfn):
            print >>sys.stderr, '%s: DOES NOT EXIST' % mdfn
            ok = False
            continue
        md5 = read_file(mdfn)
        instr += '%s  %s\n' % (md5, fn)
    if len(instr):
        cmd = "echo '%s' | md5sum -c" % instr
        rtn,out,err = run_command(cmd)
        print out, err
        if rtn:
            print >>sys.stderr, 'ERROR: return code', rtn
            print >>sys.stderr, out
            print >>sys.stderr, err
            ok = False
    return ok

def check_md5s(WISE):
    from astrometry.util.run_command import run_command
    from astrometry.util.file import read_file
    ibad = []
    for i,wise in enumerate(WISE):
        print 'Checking md5', i+1, 'of', len(WISE)
        if not check_one_md5(wise):
            ibad.append(i)
    return np.array(ibad)

def get_dir_for_coadd(outdir, coadd_id):
    # base/RRR/RRRRsDDD/unwise-*
    return os.path.join(outdir, coadd_id[:3], coadd_id)

def get_epoch_breaks(mjds):
    mjds = np.sort(mjds)

    # define an epoch either as a gap of more than 3 months
    # between frames, or as > 6 months since start of epoch.
    start = mjds[0]
    ebreaks = []
    for lastmjd,mjd in zip(mjds, mjds[1:]):
        if (mjd - lastmjd >= 90.) or (mjd - start >= 180.):
            ebreaks.append((mjd + lastmjd) / 2.)
            start = mjd
    print 'Defined epoch breaks', ebreaks
    print 'Found', len(ebreaks), 'epoch breaks'
    return ebreaks

def one_coadd(ti, band, W, H, pixscale, WISE,
              ps, wishlist, outdir, mp1, mp2, do_cube, plots2,
              frame0, nframes, force, medfilt, maxmem, do_dsky, checkmd5,
              bgmatch, center, minmax, rchi_fraction, do_cube1, epoch,
              before, after, force_outdir=False, just_image=False, version=None):
    '''
    Create coadd for one tile & band.
    '''
    print 'Coadd tile', ti.coadd_id
    print 'RA,Dec', ti.ra, ti.dec
    print 'Band', band

    wisepixscale = 2.75

    if version is None:
        from astrometry.util.run_command import run_command
        code_dir = os.path.dirname(os.path.realpath(__file__))
        cwd = os.getcwd()
        do_chdir = (cwd[0:len(code_dir)] != code_dir)
        if do_chdir:
            os.chdir(code_dir)
        rtn,version,err = run_command('git describe')
        if do_chdir:
            os.chdir(cwd)
        if rtn:
            raise RuntimeError('Failed to get version string (git describe):' + ver + err)
        version = version.strip()
    print '"git describe" version info:', version

    if not force_outdir:
        outdir = get_dir_for_coadd(outdir, ti.coadd_id)
        if not os.path.exists(outdir):
            print 'mkdir', outdir
            os.makedirs(outdir)
    tag = 'unwise-%s-w%i' % (ti.coadd_id, band)
    prefix = os.path.join(outdir, tag)
    ofn = prefix + '-img-m.fits'
    if os.path.exists(ofn):
        print 'Output file exists:', ofn
        if not force:
            return 0

    cowcs = get_coadd_tile_wcs(ti.ra, ti.dec, W, H, pixscale)
    # Intermediate world coordinates (IWC) polygon
    r,d = walk_wcs_boundary(cowcs, step=W, margin=10)
    ok,u,v = cowcs.radec2iwc(r,d)
    copoly = np.array(list(reversed(zip(u,v))))
    print 'Coadd IWC polygon:', copoly

    margin = (1.1 # safety margin
              * (np.sqrt(2.) / 2.) # diagonal
              * (max(W,H) * pixscale/3600.
                 + 1016 * wisepixscale/3600) # WISE FOV + coadd FOV side length
              ) # in deg
    t0 = Time()

    # cut
    WISE = WISE[WISE.band == band]
    WISE.cut(degrees_between(ti.ra, ti.dec, WISE.ra, WISE.dec) < margin)
    print 'Found', len(WISE), 'WISE frames in range and in band W%i' % band

    # Cut on IWC box
    ok,u,v = cowcs.radec2iwc(WISE.ra, WISE.dec)
    u0,v0 = copoly.min(axis=0)
    u1,v1 = copoly.max(axis=0)
    #print 'Coadd IWC range:', u0,u1, v0,v1
    margin = np.sqrt(2.) * (1016./2.) * (wisepixscale/3600.) * 1.01 # safety
    WISE.cut((u + margin >= u0) * (u - margin <= u1) *
             (v + margin >= v0) * (v - margin <= v1))
    print 'cut to', len(WISE), 'in RA,Dec box'

    # Use a subset of frames?
    if epoch is not None:
        ebreaks = get_epoch_breaks(WISE.mjd)
        assert(epoch <= len(ebreaks))
        if epoch > 0:
            WISE = WISE[WISE.mjd >= ebreaks[epoch - 1]]
        if epoch < len(ebreaks):
            WISE = WISE[WISE.mjd <  ebreaks[epoch]]
        print 'Cut to', len(WISE), 'within epoch'

    if bgmatch or center:
        # reorder by dist from center
        WISE.cut(np.argsort(degrees_between(ti.ra, ti.dec, WISE.ra, WISE.dec)))
    
    if ps and False:
        plt.clf()
        plt.plot(copoly[:,0], copoly[:,1], 'r-')
        plt.plot(copoly[0,0], copoly[0,1], 'ro')
        plt.plot(u, v, 'b.')
        plt.axvline(u0 - margin, color='k')
        plt.axvline(u1 + margin, color='k')
        plt.axhline(v0 - margin, color='k')
        plt.axhline(v1 + margin, color='k')
        ok,u2,v2 = cowcs.radec2iwc(WISE.ra, WISE.dec)
        plt.plot(u2, v2, 'go')
        ps.savefig()
        
    # We keep all of the input frames in the list, marking ones we're not
    # going to use, for later diagnostics.
    WISE.use = np.ones(len(WISE), bool)
    WISE.moon_rej = np.zeros(len(WISE), bool)
    WISE.use *= (WISE.qual_frame > 0)
    print 'Cut out qual_frame = 0;', sum(WISE.use), 'remaining'
    WISE.use *= (WISE.planets == 0)
    print 'Cut out planets != 0;', sum(WISE.use), 'remaining'

    if band in [3,4]:
        WISE.use *= (WISE.dtanneal > 2000.)
        print 'Cut out dtanneal <= 2000 seconds:', sum(WISE.use), 'remaining'

    if band == 4:
        ok = np.array([np.logical_or(s < '03752a', s > '03761b')
                       for s in WISE.scan_id])
        WISE.use *= ok
        print 'Cut out bad scans in W4:', sum(WISE.use), 'remaining'

    # this will need to be adapted/modified for the time-resolved coadds...
    # Cut on moon, based on (robust) measure of standard deviation
    if sum(WISE.moon_masked[WISE.use]):
        moon = WISE.moon_masked[WISE.use]
        nomoon = np.logical_not(moon)
        Imoon = np.flatnonzero(WISE.use)[moon]
        assert(sum(moon) == len(Imoon))
        print sum(nomoon), 'of', sum(WISE.use), 'frames are not moon_masked'
        nomoonstdevs = WISE.intmed16p[WISE.use][nomoon]
        med = np.median(nomoonstdevs)
        mad = 1.4826 * np.median(np.abs(nomoonstdevs - med))
        print 'Median', med, 'MAD', mad
        moonstdevs = WISE.intmed16p[WISE.use][moon]
        okmoon = (moonstdevs - med)/mad < 5.
        print sum(np.logical_not(okmoon)), 'of', len(okmoon), 'moon-masked frames have large pixel variance'
        WISE.use[Imoon] *= okmoon
        WISE.moon_rej[Imoon] = (~okmoon)
        print 'Cut to', sum(WISE.use), 'on moon'
        del Imoon
        del moon
        del nomoon
        del nomoonstdevs
        del med
        del mad
        del moonstdevs
        del okmoon

    print 'Frames:'
    for i,w in enumerate(WISE):
        print '  ', i, w.scan_id, '%4i' % w.frame_num, 'MJD', w.mjd

    if before is not None:
        WISE.cut(WISE.mjd < before)
        print 'Cut to', len(WISE), 'frames before MJD', before
    if after is not None:
        WISE.cut(WISE.mjd > after)
        print 'Cut to', len(WISE), 'frames after MJD', after
            
    if frame0 or nframes:
        i0 = frame0
        if nframes:
            WISE = WISE[frame0:frame0 + nframes]
        else:
            WISE = WISE[frame0:]
        print 'Cut to', len(WISE), 'frames starting from index', frame0
        
    if wishlist:
        for wise in WISE:
            intfn = get_l1b_file(wisedir, wise.scan_id, wise.frame_num, band)
            if not os.path.exists(intfn):
                print 'Need:', intfn
        return 0

    # Estimate memory usage and bail out if too high.
    if maxmem:
        mem = 1. + (len(WISE) * 1e6/2. * 5. / 1e9)
        print 'Estimated mem usage:', mem
        if mem > maxmem:
            print 'Estimated memory usage:', mem, 'GB > max', maxmem
            return -1

    # *inclusive* coordinates of the bounding-box in the coadd of this
    # image (x0,x1,y0,y1)
    WISE.coextent = np.zeros((len(WISE), 4), int)
    # *inclusive* coordinates of the bounding-box in the image
    # overlapping coadd
    WISE.imextent = np.zeros((len(WISE), 4), int)

    WISE.imagew = np.zeros(len(WISE), np.int)
    WISE.imageh = np.zeros(len(WISE), np.int)
    WISE.intfn  = np.zeros(len(WISE), object)
    WISE.wcs    = np.zeros(len(WISE), object)

    # count total number of coadd-space pixels -- this determines memory use
    pixinrange = 0.

    nu = 0
    NU = sum(WISE.use)
    for wi,wise in enumerate(WISE):
        if not wise.use:
            continue
        print
        nu += 1
        print nu, 'of', NU
        print 'scan', wise.scan_id, 'frame', wise.frame_num, 'band', band

        failedfiles = []
        found = False
        for wdir in wisedirs + [None]:
            download = False
            if wdir is None:
                download = True
                wdir = 'merge_p1bm_frm'

            intfn = get_l1b_file(wdir, wise.scan_id, wise.frame_num, band)
            print 'intfn', intfn
            intfnx = intfn.replace(wdir+'/', '')

            if download:
                # Try to download the file from IRSA.
                cmd = (('(wget -r -N -nH -np -nv --cut-dirs=4 -A "*w%i*" ' +
                        '"http://irsa.ipac.caltech.edu/ibe/data/wise/merge/merge_p1bm_frm/%s/")') %
                        (band, os.path.dirname(intfnx)))
                print
                print 'Trying to download file:'
                print cmd
                print
                os.system(cmd)
                print

            if os.path.exists(intfn):
                try:
                    if not int_gz:
                        wcs = Sip(intfn)
                    else:
                        tmpname = (intfn.split('/'))[-1]
                        tmpname = tmpname.replace('.gz', '')
                        # add random stuff to tmpname to avoid collisions b/w simultaneous jobs
                        tmpname = str(random.randint(0, 1000000)).zfill(7) + '-' + tmpname
                        cmd_unzip_tmp = 'gunzip -c '+ intfn + ' > ' + tmpname
                        os.system(cmd_unzip_tmp)
                        wcs = Sip(tmpname)
                        # delete unzipped temp file
                        cmd_delete_tmp = 'rm ' +  tmpname
                        os.system(cmd_delete_tmp)
                except RuntimeError:
                    import traceback
                    traceback.print_exc()
                    continue
            else:
                print 'does not exist:', intfn
                continue
            if (os.path.exists(intfn.replace('-int-', '-unc-') + ('.gz' if not int_gz else '')) and
                os.path.exists(intfn.replace('-int-', '-msk-') + ('.gz' if not int_gz else ''))):
                found = True
                break
            else:
                print 'missing unc or msk file'
                continue
        if not found:
            print 'WARNING: Not found: scan', wise.scan_id, 'frame', wise.frame_num, 'band', band
            failedfiles.append(intfnx)
            continue

        h,w = wcs.get_height(), wcs.get_width()
        r,d = walk_wcs_boundary(wcs, step=2.*w, margin=10)
        ok,u,v = cowcs.radec2iwc(r, d)
        poly = np.array(list(reversed(zip(u,v))))
        #print 'Image IWC polygon:', poly
        intersects = polygons_intersect(copoly, poly)

        if ps and False:
            plt.clf()
            plt.plot(copoly[:,0], copoly[:,1], 'r-')
            plt.plot(copoly[0,0], copoly[0,1], 'ro')
            plt.plot(poly[:,0], poly[:,1], 'b-')
            plt.plot(poly[0,0], poly[0,1], 'bo')
            cpoly = np.array(clip_polygon(copoly, poly))
            if len(cpoly) == 0:
                pass
            else:
                print 'cpoly:', cpoly
                plt.plot(cpoly[:,0], cpoly[:,1], 'm-')
                plt.plot(cpoly[0,0], cpoly[0,1], 'mo')
            ps.savefig()

        if not intersects:
            print 'Image does not intersect target'
            WISE.use[wi] = False
            continue

        cpoly = np.array(clip_polygon(copoly, poly))
        if len(cpoly) == 0:
            print 'No overlap between coadd and image polygons'
            print 'copoly:', copoly
            print 'poly:', poly
            print 'cpoly:', cpoly
            WISE.use[wi] = False
            continue

        # Convert the intersected polygon in IWC space into image
        # pixel bounds.
        # Coadd extent:
        xy = np.array([cowcs.iwc2pixelxy(u,v) for u,v in cpoly])
        xy -= 1
        x0,y0 = np.floor(xy.min(axis=0)).astype(int)
        x1,y1 = np.ceil (xy.max(axis=0)).astype(int)
        WISE.coextent[wi,:] = [np.clip(x0, 0, W-1),
                               np.clip(x1, 0, W-1),
                               np.clip(y0, 0, H-1),
                               np.clip(y1, 0, H-1)]

        # Input image extent:
        #   There was a bug in the an-ran coadds; all imextents are
        #   [0,1015,0,1015] as a result.
        #rd = np.array([cowcs.iwc2radec(u,v) for u,v in poly])
        # Should be: ('cpoly' rather than 'poly' here)
        rd = np.array([cowcs.iwc2radec(u,v) for u,v in cpoly])
        ok,x,y = np.array(wcs.radec2pixelxy(rd[:,0], rd[:,1]))
        x -= 1
        y -= 1
        x0,y0 = [np.floor(v.min(axis=0)).astype(int) for v in [x,y]]
        x1,y1 = [np.ceil (v.max(axis=0)).astype(int) for v in [x,y]]
        WISE.imextent[wi,:] = [np.clip(x0, 0, w-1),
                               np.clip(x1, 0, w-1),
                               np.clip(y0, 0, h-1),
                               np.clip(y1, 0, h-1)]

        WISE.intfn[wi] = intfn
        WISE.imagew[wi] = w
        WISE.imageh[wi] = h
        WISE.wcs[wi] = wcs
        print 'Image extent:', WISE.imextent[wi,:]
        print 'Coadd extent:', WISE.coextent[wi,:]

        # Count total coadd-space bounding-box size -- this x 5 bytes
        # is the memory toll of our round-1 coadds, which is basically
        # the peak memory use.
        e = WISE.coextent[wi,:]
        pixinrange += (1+e[1]-e[0]) * (1+e[3]-e[2])
        print 'Total pixels in coadd space:', pixinrange

    if len(failedfiles):
        print len(failedfiles), 'failed:'
        for f in failedfiles:
            print '  ', f
        print

    # Now we can make a more informed estimate of memory use.
    if maxmem:
        mem = 1. + (pixinrange * 5. / 1e9)
        print 'Estimated mem usage:', mem
        if mem > maxmem:
            print 'Estimated memory usage:', mem, 'GB > max', maxmem
            return -1

    # convert from object array to string array; '' rather than '0'
    WISE.intfn = np.array([{0:''}.get(s,s) for s in WISE.intfn])
    print 'Cut to', sum(WISE.use), 'frames intersecting target'

    t1 = Time()
    print 'Up to coadd_wise:'
    print t1 - t0

    # Now that we've got some information about the input frames, call
    # the real coadding code.  Maybe we should move this first loop into
    # the round 1 coadd...
    try:
        (coim,coiv,copp,con, coimb,coivb,coppb,conb,masks, cube, cosky,
         comin,comax,cominb,comaxb
         )= coadd_wise(ti.coadd_id, cowcs, WISE[WISE.use], ps, band, mp1, mp2, do_cube,
                       medfilt, plots2=plots2, do_dsky=do_dsky,
                       checkmd5=checkmd5, bgmatch=bgmatch, minmax=minmax,
                       rchi_fraction=rchi_fraction, do_cube1=do_cube1)
    except:
        print 'coadd_wise failed:'
        import traceback
        traceback.print_exc()
        print 'time up to failure:'
        t2 = Time()
        print t2 - t1
        return
    t2 = Time()
    print 'coadd_wise:'
    print t2 - t1

    # For any "masked" pixels that have invvar = 0 (ie, NO pixels
    # contributed), fill in the image from the "unmasked" image.
    # Leave the invvar image untouched.
    coimb[coivb == 0] = coim[coivb == 0]

    # Plug the WCS header cards into the output coadd files.
    f,wcsfn = tempfile.mkstemp()
    os.close(f)
    cowcs.write_to(wcsfn)
    hdr = fitsio.read_header(wcsfn)
    os.remove(wcsfn)

    hdr.add_record(dict(name='MAGZP', value=22.5,
                        comment='Magnitude zeropoint (in Vega mag)'))
    hdr.add_record(dict(name='UNW_SKY', value=cosky,
                        comment='Background value subtracted from coadd img'))
    hdr.add_record(dict(name='UNW_VER', value=version,
                        comment='unWISE code git revision'))
    hdr.add_record(dict(name='UNW_URL', value='https://github.com/dstndstn/unwise-coadds',
                        comment='git URL'))
    hdr.add_record(dict(name='UNW_DVER', value=2.1,
                        comment='unWISE data model version'))
    hdr.add_record(dict(name='UNW_DATE', value=datetime.datetime.now().isoformat(),
                        comment='unWISE run time'))
    hdr.add_record(dict(name='UNW_FR0', value=frame0, comment='unWISE frame start'))
    hdr.add_record(dict(name='UNW_FRN', value=nframes, comment='unWISE N frames'))
    hdr.add_record(dict(name='UNW_MEDF', value=medfilt, comment='unWISE median filter sz'))
    hdr.add_record(dict(name='UNW_BGMA', value=bgmatch, comment='unWISE background matching?'))

    # make sure there's always a numerical representation of epoch that can go into header
    if epoch is None:
        epoch_num = -1
    else:
        epoch_num = epoch

    hdr.add_record(dict(name='EPOCH', value=epoch_num, comment='epoch number'))

    # want to change .use to .included for these header keywords eventually ...
    # might crash if WISE.use is all zeros ...
    kw_mjdmin = np.min((WISE[WISE.use == 1]).mjd)
    kw_mjdmax = np.max((WISE[WISE.use == 1]).mjd)

    hdr.add_record(dict(name='MJDMIN', value=kw_mjdmin, comment='minimum MJD among included L1b frames'))
    hdr.add_record(dict(name='MJDMAX', value=kw_mjdmax, comment='maximum MJD among included L1b frames'))

    # "Unmasked" versions
    ofn = prefix + '-img-u.fits'
    fitsio.write(ofn, coim.astype(np.float32), header=hdr, clobber=True, extname='coadded image, outliers patched')
    print 'Wrote', ofn

    if just_image:
        return 0

    ofn = prefix + '-invvar-u.fits'
    fitsio.write(ofn, coiv.astype(np.float32), header=hdr, clobber=True, extname='inverse variance, outliers patched')
    print 'Wrote', ofn
    ofn = prefix + '-std-u.fits'
    fitsio.write(ofn, copp.astype(np.float32), header=hdr, clobber=True, extname='sample standard deviation, outliers patched')
    print 'Wrote', ofn
    ofn = prefix + '-n-u.fits'
    fitsio.write(ofn, con.astype(np.int16), header=hdr, clobber=True, extname='integer frame coverage, outlier pixels patched')
    print 'Wrote', ofn

    # "Masked" versions
    ofn = prefix + '-img-m.fits'
    fitsio.write(ofn, coimb.astype(np.float32), header=hdr, clobber=True, extname='coadded image, outliers removed')
    print 'Wrote', ofn
    ofn = prefix + '-invvar-m.fits'
    fitsio.write(ofn, coivb.astype(np.float32), header=hdr, clobber=True, extname='inverse variance, outliers removed')
    print 'Wrote', ofn
    ofn = prefix + '-std-m.fits'
    fitsio.write(ofn, coppb.astype(np.float32), header=hdr, clobber=True, extname='sample standard deviation, outliers removed')
    print 'Wrote', ofn
    ofn = prefix + '-n-m.fits'
    fitsio.write(ofn, conb.astype(np.int16), header=hdr, clobber=True, extname='integer frame coverage, outlier pixels removed')
    print 'Wrote', ofn

    if do_cube:
        ofn = prefix + '-cube.fits'
        fitsio.write(ofn, cube.astype(np.float32), header=hdr, clobber=True)

    if minmax:
        ofn = prefix + '-min-m.fits'
        fitsio.write(ofn, cominb.astype(np.float32), header=hdr, clobber=True)
        print 'Wrote', ofn
        ofn = prefix + '-max-m.fits'
        fitsio.write(ofn, comaxb.astype(np.float32), header=hdr, clobber=True)
        print 'Wrote', ofn
        ofn = prefix + '-min-u.fits'
        fitsio.write(ofn, comin.astype(np.float32), header=hdr, clobber=True)
        print 'Wrote', ofn
        ofn = prefix + '-max-u.fits'
        fitsio.write(ofn, comax.astype(np.float32), header=hdr, clobber=True)
        print 'Wrote', ofn

    WISE.included = np.zeros(len(WISE), bool)
    WISE.sky1 = np.zeros(len(WISE), np.float32)
    WISE.sky2 = np.zeros(len(WISE), np.float32)
    WISE.zeropoint = np.zeros(len(WISE), np.float32)
    WISE.npixoverlap = np.zeros(len(WISE), np.int32)
    WISE.npixpatched = np.zeros(len(WISE), np.int32)
    WISE.npixrchi    = np.zeros(len(WISE), np.int32)
    WISE.weight      = np.zeros(len(WISE), np.float32)

    Iused = np.flatnonzero(WISE.use)
    assert(len(Iused) == len(masks))

    maskdir = os.path.join(outdir, tag + '-mask')
    if not os.path.exists(maskdir):
        os.mkdir(maskdir)
            
    for i,mm in enumerate(masks):
        if mm is None:
            continue

        ii = Iused[i]
        WISE.sky1       [ii] = mm.sky
        WISE.sky2       [ii] = mm.dsky
        WISE.zeropoint  [ii] = mm.zp
        WISE.npixoverlap[ii] = mm.ncopix
        WISE.npixpatched[ii] = mm.npatched
        WISE.npixrchi   [ii] = mm.nrchipix
        WISE.weight     [ii] = mm.w

        if not mm.included:
            continue

        WISE.included   [ii] = True

        # Write outlier masks
        ofn = WISE.intfn[ii].replace('-int', '')
        ofn = os.path.join(maskdir, 'unwise-mask-' + ti.coadd_id + '-'
                           + os.path.basename(ofn) + ('.gz' if not int_gz else ''))
        w,h = WISE.imagew[ii],WISE.imageh[ii]
        fullmask = np.zeros((h,w), mm.omask.dtype)
        x0,x1,y0,y1 = WISE.imextent[ii,:]
        fullmask[y0:y1+1, x0:x1+1] = mm.omask
        fitsio.write(ofn, fullmask, clobber=True)
        print 'Wrote mask', (i+1), 'of', len(masks), ':', ofn

    WISE.delete_column('wcs')

    # downcast datatypes, and work around fitsio's issues with
    # "bool" columns
    for c,t in [('included', np.uint8),
                ('use', np.uint8),
                ('moon_masked', np.uint8),
                ('moon_rej', np.uint8),
                ('imagew', np.int16),
                ('imageh', np.int16),
                ('coextent', np.int16),
                ('imextent', np.int16),
                ]:
        WISE.set(c, WISE.get(c).astype(t))

    ofn = prefix + '-frames.fits'
    WISE.writeto(ofn)
    print 'Wrote', ofn

    md = tag + '-mask'
    cmd = ('cd %s && tar czf %s %s && rm -R %s' %
           (outdir, md + '.tgz', md, md))
    print 'tgz:', cmd
    rtn,out,err = run_command(cmd)
    print out, err
    if rtn:
        print >>sys.stderr, 'ERROR: return code', rtn
        print >>sys.stderr, 'Command:', cmd
        print >>sys.stderr, out
        print >>sys.stderr, err
        ok = False

    return 0

def plot_region(r0,r1,d0,d1, ps, T, WISE, wcsfns, W, H, pixscale, margin=1.05,
                allsky=False, grid_ra_range=None, grid_dec_range=None,
                grid_spacing=[5, 5, 20, 10], label_tiles=True, draw_outline=True,
                tiles=[], ra=0., dec=0.):
    from astrometry.blind.plotstuff import Plotstuff
    maxcosdec = np.cos(np.deg2rad(min(abs(d0),abs(d1))))
    if allsky:
        W,H = 1000,500
        plot = Plotstuff(outformat='png', size=(W,H))
        plot.wcs = anwcs_create_allsky_hammer_aitoff(ra, dec, W, H)
    else:
        plot = Plotstuff(outformat='png', size=(800,800),
                         rdw=((r0+r1)/2., (d0+d1)/2., margin*max(d1-d0, (r1-r0)*maxcosdec)))

    plot.fontsize = 10
    plot.halign = 'C'
    plot.valign = 'C'

    for i in range(3):
        if i in [0,2]:
            plot.color = 'verydarkblue'
        else:
            plot.color = 'black'
        plot.plot('fill')
        plot.color = 'white'
        out = plot.outline

        if i == 0:
            if T is None:
                continue
            print 'plot 0'
            for i,ti in enumerate(T):
                cowcs = get_coadd_tile_wcs(ti.ra, ti.dec, W, H, pixscale)
                plot.alpha = 0.5
                out.wcs = anwcs_new_tan(cowcs)
                out.fill = 1
                plot.plot('outline')
                out.fill = 0
                plot.plot('outline')

                if label_tiles:
                    plot.alpha = 1.
                    rc,dc = cowcs.radec_center()
                    plot.text_radec(rc, dc, '%i' % i)

        elif i == 1:
            if WISE is None:
                continue
            print 'plot 1'
            # cut
            #WISE = WISE[WISE.band == band]
            plot.alpha = (3./256.)
            out.fill = 1
            print 'Plotting', len(WISE), 'exposures'
            wcsparams = []
            fns = []
            for wi,wise in enumerate(WISE):
                if wi % 10 == 0:
                    print '.',
                if wi % 1000 == 0:
                    print wi, 'of', len(WISE)

                if wi and wi % 10000 == 0:
                    fn = ps.getnext()
                    plot.write(fn)
                    print 'Wrote', fn

                    wp = np.array(wcsparams)
                    WW = fits_table()
                    WW.crpix  = wp[:, 0:2]
                    WW.crval  = wp[:, 2:4]
                    WW.cd     = wp[:, 4:8]
                    WW.imagew = wp[:, 8]
                    WW.imageh = wp[:, 9]
                    WW.intfn = np.array(fns)
                    WW.writeto('sequels-wcs.fits')

                intfn = get_l1b_file(wisedir, wise.scan_id, wise.frame_num, wise.band)
                try:
                    # what happens here when int_gz is true ???
                    wcs = Tan(intfn, 0, 1)
                except:
                    import traceback
                    traceback.print_exc()
                    continue
                out.wcs = anwcs_new_tan(wcs)
                plot.plot('outline')

                wcsparams.append((wcs.crpix[0], wcs.crpix[1], wcs.crval[0], wcs.crval[1],
                                  wcs.cd[0], wcs.cd[1], wcs.cd[2], wcs.cd[3],
                                  wcs.imagew, wcs.imageh))
                fns.append(intfn)

            wp = np.array(wcsparams)
            WW = fits_table()
            WW.crpix  = wp[:, 0:2]
            WW.crval  = wp[:, 2:4]
            WW.cd     = wp[:, 4:8]
            WW.imagew = wp[:, 8]
            WW.imageh = wp[:, 9]
            WW.intfn = np.array(fns)
            WW.writeto('sequels-wcs.fits')

            fn = ps.getnext()
            plot.write(fn)
            print 'Wrote', fn

        elif i == 2:
            print 'plot 2'
            if wcsfns is None:
                print 'wcsfns is none'
                continue
            print 'wcsfns:', len(wcsfns), 'tiles', len(tiles)
            plot.alpha = 0.5
            for fn in wcsfns:
                out.set_wcs_file(fn, 0)
                out.fill = 1
                plot.plot('outline')
                out.fill = 0
                plot.plot('outline')

            for it,tile in enumerate(tiles):
                if it % 1000 == 0:
                    print 'plotting tile', tile
                ra,dec = tile_to_radec(tile)
                wcs = get_coadd_tile_wcs(ra, dec)
                out.wcs = anwcs_new_tan(wcs)
                out.fill = 1
                plot.plot('outline')
                out.fill = 0
                plot.plot('outline')

        plot.color = 'gray'
        plot.alpha = 1.
        grid = plot.grid
        grid.ralabeldir = 2

        if grid_ra_range is not None:
            grid.ralo, grid.rahi = grid_ra_range
        if grid_dec_range is not None:
            grid.declo, grid.dechi = grid_dec_range
        plot.plot_grid(*grid_spacing)

        if draw_outline:
            plot.color = 'red'
            plot.apply_settings()
            plot.line_constant_dec(d0, r0, r1)
            plot.stroke()
            plot.line_constant_ra(r1, d0, d1)
            plot.stroke()
            plot.line_constant_dec(d1, r1, r0)
            plot.stroke()
            plot.line_constant_ra(r0, d1, d0)
            plot.stroke()
        fn = ps.getnext()
        plot.write(fn)
        print 'Wrote', fn


def _bounce_one_round2(*A):
    try:
        return _coadd_one_round2(*A)
    except:
        import traceback
        print '_coadd_one_round2 failed:'
        traceback.print_exc()
        raise

def _coadd_one_round2((ri, N, scanid, rr, cow1, cowimg1, cowimgsq1, tinyw,
                       plotfn, ps1, do_dsky, rchi_fraction)):
    '''
    For multiprocessing, the function to be called for each round-2
    frame.
    '''
    if rr is None:
        return None
    print 'Coadd round 2, image', (ri+1), 'of', N
    t00 = Time()
    mm = Duck()
    mm.npatched = rr.npatched
    mm.ncopix   = rr.ncopix
    mm.sky      = rr.sky
    mm.zp       = rr.zp
    mm.w        = rr.w
    mm.included = True

    cox0,cox1,coy0,coy1 = rr.coextent
    coslc = slice(coy0, coy1+1), slice(cox0, cox1+1)
    # Remove this image from the per-pixel std calculation...
    subw  = np.maximum(cow1[coslc] - rr.w, tinyw)
    subco = (cowimg1  [coslc] - (rr.w * rr.rimg   )) / subw
    subsq = (cowimgsq1[coslc] - (rr.w * rr.rimg**2)) / subw
    subv = np.maximum(0, subsq - subco**2)
    # previously, no prior:
    # subp = np.sqrt(np.maximum(0, subsq - subco**2))

    # "prior" estimate of per-pixel noise: sig1 + 3% flux in quadrature
    # rr.w = 1./sig1**2 for this image.
    priorv = 1./rr.w + (0.03 * np.maximum(subco, 0))**2
    # Weight that prior equal to the 'subv' estimate from nprior exposures
    nprior = 5
    priorw = nprior * rr.w
    subpp = np.sqrt((subv * subw + priorv * priorw) / (subw + priorw))
    
    mask = (rr.rmask & 1).astype(bool)

    # like in the WISE Atlas Images, estimate sky difference via
    # median difference in the overlapping area.
    if do_dsky:
        dsky = median_f((rr.rimg[mask] - subco[mask]).astype(np.float32))
        print 'Sky difference:', dsky
    else:
        dsky = 0.

    rchi = ((rr.rimg - dsky - subco) * mask * (subw > 0) * (subpp > 0) /
            np.maximum(subpp, 1e-6))
    assert(np.all(np.isfinite(rchi)))

    badpix = (np.abs(rchi) >= 5.)
    #print 'Number of rchi-bad pixels:', np.count_nonzero(badpix)

    mm.nrchipix = np.count_nonzero(badpix)

    # Bit 1: abs(rchi) >= 5
    badpixmask = badpix.astype(np.uint8)
    # grow by a small margin
    badpix = binary_dilation(badpix)
    # Bit 2: grown
    badpixmask += (2 * badpix)
    # Add rchi-masked pixels to the mask
    # (clear bit 2)
    rr.rmask[badpix] &= ~2
    mm.omask = np.zeros((rr.wcs.get_height(), rr.wcs.get_width()),
                        badpixmask.dtype)
    try:
        Yo,Xo,Yi,Xi,nil = resample_with_wcs(rr.wcs, rr.cosubwcs, [], None)
        mm.omask[Yo,Xo] = badpixmask[Yi,Xi]
    except OverlapError:
        import traceback
        print 'WARNING: Caught OverlapError resampling rchi mask'
        print 'rr WCS', rr.wcs
        print 'shape', mm.omask.shape
        print 'cosubwcs:', rr.cosubwcs
        traceback.print_exc(None, sys.stdout)

    if mm.nrchipix > mm.ncopix * rchi_fraction:
        print ('WARNING: dropping exposure %s: n rchi pixels %i / %i' %
               (scanid, mm.nrchipix, mm.ncopix))
        mm.included = False

    if ps1:
        # save for later
        mm.rchi = rchi
        mm.badpix = badpix
        if mm.included:
            mm.rimg_orig = rr.rimg.copy()
            mm.rmask_orig = rr.rmask.copy()

    if mm.included:
        ok = patch_image(rr.rimg, np.logical_not(badpix),
                         required=(badpix * mask))
        if not ok:
            print 'patch_image failed'
            return None

        rimg = (rr.rimg - dsky)
        mm.coslc = coslc
        mm.coimgsq = mask * rr.w * rimg**2
        mm.coimg   = mask * rr.w * rimg
        mm.cow     = mask * rr.w
        mm.con     = mask
        mm.rmask2  = (rr.rmask & 2).astype(bool)

    mm.dsky = dsky / rr.zpscale
        
    if plotfn:
        # HACK
        rchihistrange = 6
        rchihistargs = dict(range=(-rchihistrange,rchihistrange), bins=100)
        rchihist = None
        rchihistedges = None

        R,C = 3,3
        plt.clf()
        I = rr.rimg - dsky
        # print 'rimg shape', rr.rimg.shape
        # print 'rmask shape', rr.rmask.shape
        # print 'rmask elements set:', np.sum(rr.rmask)
        # print 'len I[rmask]:', len(I[rr.rmask])
        mask = (rr.rmask & 1).astype(bool)
        if len(I[mask]):
            plt.subplot(R,C,1)
            plo,phi = [np.percentile(I[mask], p) for p in [25,99]]
            plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=plo, vmax=phi)
            plt.xticks([]); plt.yticks([])
            plt.title('rimg')

        plt.subplot(R,C,2)
        I = subco
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('subco')
        plt.subplot(R,C,3)
        I = subpp
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('subpp')
        plt.subplot(R,C,4)
        plt.imshow(rchi, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=-5, vmax=5)
        plt.xticks([]); plt.yticks([])
        plt.title('rchi (%i)' % mm.nrchipix)

        plt.subplot(R,C,8)
        plt.imshow(np.abs(rchi) >= 5., interpolation='nearest', origin='lower',
                   cmap='gray', vmin=0, vmax=1)
        plt.xticks([]); plt.yticks([])
        plt.title('bad rchi')

        plt.subplot(R,C,5)
        I = rr.img
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('img')

        plt.subplot(R,C,6)
        I = mm.omask
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=0, vmax=3)
        plt.xticks([]); plt.yticks([])
        plt.title('omask')

        plt.subplot(R,C,7)
        I = rr.rimg
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('patched rimg')

        # plt.subplot(R,C,8)
        # I = (coimgb / np.maximum(cowb, tinyw))
        # plo,phi = [np.percentile(I, p) for p in [25,99]]
        # plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
        #            vmin=plo, vmax=phi)
        # plt.xticks([]); plt.yticks([])
        # plt.title('coimgb')

        I = (rchi != 0.)
        n,e = np.histogram(np.clip(rchi[I], -rchihistrange, rchihistrange),
                           **rchihistargs)
        if rchihist is None:
            rchihist, rchihistedges = n,e
        else:
            rchihist += n

        plt.subplot(R,C,9)
        e = rchihistedges
        e = (e[:-1]+e[1:])/2.
        #plt.semilogy(e, np.maximum(0.1, rchihist), 'b-')
        plt.semilogy(e, np.maximum(0.1, n), 'b-')
        plt.axvline(5., color='r')
        plt.xlim(-(rchihistrange+1), rchihistrange+1)
        plt.yticks([])
        plt.title('rchi')

        inc = ''
        if not mm.included:
            inc = '(not incl)'
        plt.suptitle('%s %s' % (scanid, inc))
        plt.savefig(plotfn)

    print Time() - t00
    return mm

class coaddacc():
    '''Second-round coadd accumulator.'''
    def __init__(self, H,W, do_cube=False, nims=0, bgmatch=False,
                 minmax=False):
        self.coimg    = np.zeros((H,W))
        self.coimgsq  = np.zeros((H,W))
        self.cow      = np.zeros((H,W))
        self.con      = np.zeros((H,W), np.int16)
        self.coimgb   = np.zeros((H,W))
        self.coimgsqb = np.zeros((H,W))
        self.cowb     = np.zeros((H,W))
        self.conb     = np.zeros((H,W), np.int16)

        self.bgmatch = bgmatch

        self.minmax = minmax
        if minmax:
            self.comin  = np.empty((H,W))
            self.comax  = np.empty((H,W))
            self.cominb = np.empty((H,W))
            self.comaxb = np.empty((H,W))
            self.comin [:,:] =  1e30
            self.cominb[:,:] =  1e30
            self.comax [:,:] = -1e30
            self.comaxb[:,:] = -1e30
        else:
            self.comin  = None
            self.comax  = None
            self.cominb = None
            self.comaxb = None

        if do_cube:
            self.cube = np.zeros((nims, H, W), np.float32)
            self.cubei = 0
        else:
            self.cube = None

    def finish(self):
        if self.minmax:
            # Set pixels that weren't changed from their initial values to zero.
            self.comin [self.comin  ==  1e30] = 0.
            self.cominb[self.cominb ==  1e30] = 0.
            self.comax [self.comax  == -1e30] = 0.
            self.comaxb[self.comaxb == -1e30] = 0.
            
    def acc(self, mm, delmm=False):
        if mm is None or not mm.included:
            return

        if self.bgmatch:
            pass

        self.coimgsq [mm.coslc] += mm.coimgsq
        self.coimg   [mm.coslc] += mm.coimg
        self.cow     [mm.coslc] += mm.cow
        self.con     [mm.coslc] += mm.con
        self.coimgsqb[mm.coslc] += mm.rmask2 * mm.coimgsq
        self.coimgb  [mm.coslc] += mm.rmask2 * mm.coimg
        self.cowb    [mm.coslc] += mm.rmask2 * mm.cow
        self.conb    [mm.coslc] += mm.rmask2 * mm.con
        if self.cube is not None:
            self.cube[(self.cubei,) + mm.coslc] = (mm.coimg).astype(self.cube.dtype)
            self.cubei += 1
        if self.minmax:

            print 'mm.coslc:', mm.coslc
            print 'mm.con:', np.unique(mm.con), mm.con.dtype
            print 'mm.rmask2:', np.unique(mm.rmask2), mm.rmask2.dtype

            self.comin[mm.coslc][mm.con] = np.minimum(self.comin[mm.coslc][mm.con],
                                                      mm.coimg[mm.con] / mm.w)
            self.comax[mm.coslc][mm.con] = np.maximum(self.comax[mm.coslc][mm.con],
                                                      mm.coimg[mm.con] / mm.w)
            self.cominb[mm.coslc][mm.rmask2] = np.minimum(self.cominb[mm.coslc][mm.rmask2],
                                                          mm.coimg[mm.rmask2] / mm.w)
            self.comaxb[mm.coslc][mm.rmask2] = np.maximum(self.comaxb[mm.coslc][mm.rmask2],
                                                          mm.coimg[mm.rmask2] / mm.w)

            print 'comin',  self.comin.min(),  self.comin.max()
            print 'comax',  self.comax.min(),  self.comax.max()
            print 'cominb', self.cominb.min(), self.cominb.max()
            print 'comaxb', self.comaxb.min(), self.comaxb.max()

        if delmm:
            del mm.coimgsq
            del mm.coimg
            del mm.cow
            del mm.con
            del mm.rmask2


def binimg(img, b):
    hh,ww = img.shape
    hh = int(hh / b) * b
    ww = int(ww / b) * b
    return (reduce(np.add, [img[i/b:hh:b, i%b:ww:b] for i in range(b*b)]) /
            float(b*b))

def coadd_wise(tile, cowcs, WISE, ps, band, mp1, mp2,
               do_cube, medfilt, plots2=False, table=True, do_dsky=False,
               checkmd5=False, bgmatch=False, minmax=False, rchi_fraction=0.01, do_cube1=False):
    L = 3
    W = cowcs.get_width()
    H = cowcs.get_height()
    # For W4, single-image ww is ~ 1e-10
    tinyw = 1e-16

    # Round-1 coadd:
    (rimgs, coimg1, cow1, coppstd1, cowimgsq1, cube1)= _coadd_wise_round1(
        cowcs, WISE, ps, band, table, L, tinyw, mp1, medfilt, checkmd5,
        bgmatch, do_cube1)
    cowimg1 = coimg1 * cow1
    assert(len(rimgs) == len(WISE))

    if mp1 != mp2:
        print 'Shutting down multiprocessing pool 1'
        mp1.close()

    if do_cube1:
        ofn = '%s-w%i-cube1.fits' % (tile, band)
        fitsio.write(ofn, cube1, clobber=True)
        print 'Wrote', ofn

        ofn = '%s-w%i-coimg1.fits' % (tile, band)
        fitsio.write(ofn, coimg1, clobber=True)
        print 'Wrote', ofn

        ofn = '%s-w%i-cow1.fits' % (tile, band)
        fitsio.write(ofn, cow1, clobber=True)
        print 'Wrote', ofn

        ofn = '%s-w%i-coppstd1.fits' % (tile, band)
        fitsio.write(ofn, coppstd1, clobber=True)
        print 'Wrote', ofn

    if ps:
        # Plot round-one images
        plt.figure(figsize=(8,8))

        # these large subplots were causing memory errors on carver...
        grid = False
        if not grid:
            plt.figure(figsize=(4,4))

        plt.subplots_adjust(left=0.01, right=0.99, bottom=0.01, top=0.99,
                            hspace=0.05, wspace=0.05)
        #plt.subplots_adjust(left=0.01, right=0.99, bottom=0.01, top=0.9,
        #                    hspace=0.05, wspace=0.05)

        if True:
            ngood = len([x for x in rimgs if x is not None])
            cols = int(np.ceil(np.sqrt(float(ngood))))
            rows = int(np.ceil(ngood / float(cols)))
            print 'ngood', ngood, 'rows,cols', rows,cols

            if medfilt:
                sum_medfilt = np.zeros((H,W))
                sum_medfilt2 = np.zeros((H,W))
                n_medfilt = np.zeros((H,W), int)

                for rr in rimgs:
                    if rr is None:
                        continue
                    cox0,cox1,coy0,coy1 = rr.coextent
                    slc = slice(coy0,coy1+1), slice(cox0,cox1+1)

                    sum_medfilt [slc] += rr.rmedfilt
                    sum_medfilt2[slc] += rr.rmedfilt**2
                    n_medfilt   [slc][(rr.rmask & 1)>0] += 1

                mean_medfilt = sum_medfilt / n_medfilt
                std_medfilt = np.sqrt(sum_medfilt2 / n_medfilt - mean_medfilt**2)

                plt.clf()
                plt.imshow(mean_medfilt, interpolation='nearest', origin='lower')
                plt.colorbar()
                plt.title('Mean median filter')
                ps.savefig()

                plt.clf()
                plt.imshow(std_medfilt, interpolation='nearest', origin='lower')
                plt.colorbar()
                plt.title('Median filter standard deviation')
                ps.savefig()


        if False:
            stacks = []

            stack1 = []
            stack2 = []
            stack3 = []
            stack4 = []
            stack5 = []
            stack6 = []
            for j,rr in enumerate(rimgs):
                if rr is None:
                    continue

                sig1 = np.sqrt(1./rr.w)
                kwa = dict(interpolation='nearest', origin='lower',
                           vmin=-2.*sig1, vmax=3.*sig1, cmap='gray')
                rkwa = kwa.copy()
                rkwa.update(extent=rr.coextent)

                for shim,st,skwa in [(rr.rimg, stack1, rkwa),
                                     (rr.img,  stack2, kwa )]:
                    h,w = shim.shape
                    b = int(max(w,h) / 256)
                    if b>1:
                        shim = binimg(shim, b)
                    st.append((shim, skwa))
                if medfilt:
                    med = median_f(rr.medfilt.astype(np.float32).ravel())

                    for shim,st,skwa in [(rr.medfilt - med, stack3, kwa),
                                         (rr.medfilt - med + rr.img, stack4, kwa),
                                         (rr.rmedfilt, stack5, rkwa),
                                         (rr.rmedfilt + rr.rimg, stack6, rkwa)]:
                        h,w = shim.shape
                        b = int(max(w,h) / 256)
                        if b>1:
                            shim = binimg(shim, b)
                        st.append((shim, skwa))
                    
            stacks.append(stack2)
            if medfilt:
                stacks.append(stack3)
                stacks.append(stack4)
            stacks.append(stack1)
            if medfilt:
                stacks.append(stack5)
                stacks.append(stack6)

            if grid:
                for stack in stacks:
                    plt.clf()
                    for i,(im,kwa) in enumerate(stack):
                        plt.subplot(rows, cols, i+1)
                        plt.imshow(im, **kwa)
                        plt.xticks([]); plt.yticks([])
                    ps.savefig()
            else:
                # for stack in stacks:
                #     for i,(im,kwa) in enumerate(stack):
                #         plt.clf()
                #         plt.imshow(im, **kwa)
                #         plt.colorbar()
                #         plt.xticks([]); plt.yticks([])
                #         ps.savefig()
                #s1,s2,s3,s4,s5,s6 = stacks
                for i in range(len(stacks[0])):
                    plt.clf()
                    for j,stack in enumerate(stacks):
                        plt.subplot(2,3, j+1)
                        im,kwa = stack[i]
                        plt.imshow(im, **kwa)
                        if j >= 3:
                            plt.axis([0, W, 0, H])
                        plt.xticks([]); plt.yticks([])
                    ps.savefig()

            plt.clf()
            ploti = 0
            for j,rr in enumerate(rimgs):
                if rr is None:
                    continue
                fullimg = fitsio.read(WISE.intfn[j])
                fullimg -= rr.sky
                ploti += 1
                plt.subplot(rows, cols, ploti)
                print 'zpscale', rr.zpscale
                sig1 = np.sqrt(1./rr.w) / rr.zpscale
                plt.imshow(fullimg, interpolation='nearest', origin='lower',
                           vmin=-2.*sig1, vmax=3.*sig1)
                plt.xticks([]); plt.yticks([])
            ps.savefig()
    
            plt.clf()
            ploti = 0
            for j,rr in enumerate(rimgs):
                if rr is None:
                    continue
                fullimg = fitsio.read(WISE.intfn[j])
                binned = reduce(np.add, [fullimg[i/4::4, i%4::4] for i in range(16)])
                binned /= 16.
                binned -= rr.sky
                ploti += 1
                plt.subplot(rows, cols, ploti)
                sig1 = np.sqrt(1./rr.w) / rr.zpscale
                plt.imshow(binned, interpolation='nearest', origin='lower',
                           vmin=-2.*sig1, vmax=3.*sig1)
                plt.xticks([]); plt.yticks([])
            ps.savefig()

        # Plots of round-one per-image results.
        plt.figure(figsize=(4,4))
        plt.subplots_adjust(left=0.01, right=0.99, bottom=0.01, top=0.99)
        ngood = 0
        for i,rr in enumerate(rimgs):
            if ngood >= 5:
                break
            if rr is None:
                continue
            if rr.ncopix < 0.25 * W*H:
                continue
            ngood += 1
            print 'Plotting rr', i
            plt.clf()
            cim = np.zeros((H,W))
            # Make untouched pixels white.
            cim += 1e10
            cox0,cox1,coy0,coy1 = rr.coextent
            slc = slice(coy0,coy1+1), slice(cox0,cox1+1)
            mask = (rr.rmask & 1).astype(bool)
            cim[slc][mask] = rr.rimg[mask]
            sig1 = 1./np.sqrt(rr.w)
            plt.imshow(cim, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=-1.*sig1, vmax=5.*sig1)
            ps.savefig()

            cmask = np.zeros((H,W), bool)
            cmask[slc] = mask
            plt.clf()
            # invert
            plt.imshow(cmask, interpolation='nearest', origin='lower',
                       cmap='gray', vmin=0, vmax=1)
            ps.savefig()

            mask2 = (rr.rmask & 2).astype(bool)
            cmask[slc] = mask2
            plt.clf()
            plt.imshow(cmask, interpolation='nearest', origin='lower',
                       cmap='gray', vmin=0, vmax=1)
            ps.savefig()

        sig1 = 1./np.sqrt(np.median(cow1))
        plt.clf()
        plt.imshow(coimg1, interpolation='nearest', origin='lower',
                   cmap='gray', vmin=-1.*sig1, vmax=5.*sig1)
        ps.savefig()

        plt.clf()
        plt.imshow(cow1, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=0, vmax=cow1.max())
        ps.savefig()

        coppstd  = np.sqrt(np.maximum(0, cowimgsq1  /
                                      (np.maximum(cow1,  tinyw)) - coimg1**2))
        mx = np.percentile(coppstd.ravel(), 99)
        plt.clf()
        plt.imshow(coppstd, interpolation='nearest', origin='lower',
                   cmap='gray', vmin=0, vmax=mx)
        ps.savefig()


    plt.figure(figsize=(8,6))

    # If we're not multiprocessing, do the loop manually to reduce
    # memory usage (so we don't need to keep all "rr" inputs and
    # "masks" outputs in memory at once).
    t0 = Time()
    print 'Before garbage collection:', Time()-t0
    gc.collect()
    print 'After garbage collection:', Time()-t0
    ps1 = (ps is not None)
    delmm = (ps is None)
    if not mp2.pool:
        coadd = coaddacc(H, W, do_cube=do_cube, nims=len(rimgs), minmax=minmax)
        masks = []
        ri = -1
        while len(rimgs):
            ri += 1
            rr = rimgs.pop(0)
            if ps and plots2:
                plotfn = ps.getnext()
            else:
                plotfn = None
            scanid = ('scan %s frame %i band %i' %
                      (WISE.scan_id[ri], WISE.frame_num[ri], band))
            mm = _coadd_one_round2(
                (ri, len(WISE), scanid, rr, cow1, cowimg1, cowimgsq1, tinyw,
                 plotfn, ps1, do_dsky, rchi_fraction))
            coadd.acc(mm, delmm=delmm)
            masks.append(mm)
    else:
        args = []
        N = len(WISE)
        for ri,rr in enumerate(rimgs):
            if ps and plots2:
                plotfn = ps.getnext()
            else:
                plotfn = None
            scanid = ('scan %s frame %i band %i' %
                      (WISE.scan_id[ri], WISE.frame_num[ri], band))
            args.append((ri, N, scanid, rr, cow1, cowimg1, cowimgsq1, tinyw,
                         plotfn, ps1, do_dsky, rchi_fraction))
        #masks = mp.map(_coadd_one_round2, args)
        masks = mp2.map(_bounce_one_round2, args)
        del args
        print 'Accumulating second-round coadds...'
        t0 = Time()
        coadd = coaddacc(H, W, do_cube=do_cube, nims=len(rimgs), bgmatch=bgmatch,
                         minmax=minmax)
        for mm in masks:
            coadd.acc(mm, delmm=delmm)
        print Time()-t0

    coadd.finish()

    t0 = Time()
    print 'Before garbage collection:', Time()-t0
    gc.collect()
    print 'After garbage collection:', Time()-t0

    if ps:
        ngood = 0
        for i,mm in enumerate(masks):
            if ngood >= 5:
                break
            if mm is None or not mm.included:
                continue
            if sum(mm.badpix) == 0:
                continue
            if mm.ncopix < 0.25 * W*H:
                continue
            ngood += 1

            print 'Plotting mm', i

            cim = np.zeros((H,W))
            cim += 1e6
            cim[mm.coslc][mm.rmask_orig] = mm.rimg_orig[mm.rmask_orig]
            w = np.max(mm.cow)
            sig1 = 1./np.sqrt(w)

            cbadpix = np.zeros((H,W))
            cbadpix[mm.coslc][mm.con] = mm.badpix[mm.con]
            blobs,nblobs = label(cbadpix, np.ones((3,3),int))
            blobcms = center_of_mass(cbadpix, labels=blobs,
                                     index=range(nblobs+1))
            plt.clf()
            plt.imshow(cim, interpolation='nearest', origin='lower',
                       cmap='gray', vmin=-1.*sig1, vmax=5.*sig1)
            ax = plt.axis()
            for y,x in blobcms:
                plt.plot(x, y, 'o', mec='r', mew=2, mfc='none', ms=15)
            plt.axis(ax)
            ps.savefig()

            # cim[mm.coslc][mm.rmask_orig] = (mm.rimg_orig[mm.rmask_orig] -
            #                                 coimg1[mm.rmask_orig])
            # plt.clf()
            # plt.imshow(cim, interpolation='nearest', origin='lower',
            #            cmap='gray', vmin=-3.*sig1, vmax=3.*sig1)
            # ps.savefig()

            crchi = np.zeros((H,W))
            crchi[mm.coslc] = mm.rchi
            plt.clf()
            plt.imshow(crchi, interpolation='nearest', origin='lower',
                       cmap='gray', vmin=-5, vmax=5)
            ps.savefig()

            cbadpix[:,:] = 0.5
            cbadpix[mm.coslc][mm.con] = (1 - mm.badpix[mm.con])
            plt.clf()
            plt.imshow(cbadpix, interpolation='nearest', origin='lower',
                       cmap='gray', vmin=0, vmax=1)
            ps.savefig()

    coimg    = coadd.coimg
    coimgsq  = coadd.coimgsq
    cow      = coadd.cow
    con      = coadd.con
    coimgb   = coadd.coimgb
    coimgsqb = coadd.coimgsqb
    cowb     = coadd.cowb
    conb     = coadd.conb
    cube     = coadd.cube

    coimg /= np.maximum(cow, tinyw)
    coinvvar = cow

    coimgb /= np.maximum(cowb, tinyw)
    coinvvarb = cowb

    # per-pixel variance
    coppstd  = np.sqrt(np.maximum(0, coimgsq  / 
                                  np.maximum(cow,  tinyw) - coimg **2))
    coppstdb = np.sqrt(np.maximum(0, coimgsqb /
                                  np.maximum(cowb, tinyw) - coimgb**2))

    # normalize by number of frames to produce an estimate of the
    # stddev in the *coadd* rather than in the individual frames.
    # This is the sqrt of the unbiased estimator of the variance
    coppstd  /= np.sqrt(np.maximum(1., (con  - 1).astype(float)))
    coppstdb /= np.sqrt(np.maximum(1., (conb - 1).astype(float)))

    # re-estimate and subtract sky from the coadd.  approx median:
    #med = median_f(coimgb[::4,::4].astype(np.float32))
    #sig1 = 1./np.sqrt(median_f(coinvvarb[::4,::4].astype(np.float32)))
    try:
        sky = estimate_mode(coimgb)
        #sky = estimate_sky(coimgb, med-2.*sig1, med+1.*sig1, omit=None)
        print 'Estimated coadd sky:', sky
        coimg  -= sky
        coimgb -= sky
    except np.linalg.LinAlgError:
        print 'WARNING: Failed to estimate sky in coadd:'
        import traceback
        traceback.print_exc()
        sky = 0.


    if ps:
        plt.clf()
        I = coimg1
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 1')
        ps.savefig()

        plt.clf()
        I = coppstd1
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd per-pixel std 1')
        ps.savefig()

        plt.clf()
        I = cow1 / np.median([mm.w for mm in masks if mm is not None])
        plo,phi = I.min(), I.max()
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd weights 1 / median w')
        ps.savefig()

        # approx!
        con1 = np.round(I).astype(int)

        plt.clf()
        I = con
        plo,phi = I.min(), I.max()
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2: N frames')
        ps.savefig()

        plt.clf()
        I = conb
        plo,phi = I.min(), I.max()
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2: N frames (masked)')
        ps.savefig()

        plt.clf()
        I = con1 - con
        plo,phi = I.min(), I.max()
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd N round 1 - N round 2')
        ps.savefig()

        plt.clf()
        I = con1 - conb
        plo,phi = I.min(), I.max()
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd N round 1 - N round 2 (masked)')
        ps.savefig()


        plt.clf()
        I = coimg
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2')
        ps.savefig()

        plt.clf()
        I = coimgb
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 (weighted)')
        ps.savefig()


        imlo,imhi = plo,phi

        if minmax:
            for I,tt in [(coadd.comin, 'min'), (coadd.comax, 'max'), ((coadd.comin + coadd.comax)/2., 'mean(min,max)'),
                         (coadd.cominb, 'min (weighted)'), (coadd.comaxb, 'max (weighted)'),
                         ((coadd.cominb + coadd.comaxb)/2., 'mean(min,max), weighted')]:
                plt.clf()
                plt.imshow(I - sky, interpolation='nearest', origin='lower', cmap='gray',
                           vmin=plo, vmax=phi)
                plt.colorbar()
                plt.title('Coadd %s' % tt)
                ps.savefig()

            plt.clf()
            plt.imshow(((coimg * con) - (coadd.comin-sky) - (coadd.comax-sky)) / np.maximum(1, con-2),
                       interpolation='nearest', origin='lower', cmap='gray',
                       vmin=plo, vmax=phi)
            plt.colorbar()
            plt.title('Coadd - min,max')
            ps.savefig()

            plt.clf()
            plt.imshow(((coimgb * conb) - (coadd.cominb-sky) - (coadd.comaxb-sky)) / np.maximum(1, conb-2),
                       interpolation='nearest', origin='lower', cmap='gray',
                       vmin=plo, vmax=phi)
            plt.colorbar()
            plt.title('Coadd - min,max (weighted)')
            ps.savefig()

        plt.clf()
        I = coppstd
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 per-pixel std')
        ps.savefig()

        plt.clf()
        I = coppstdb
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 per-pixel std (weighted)')
        ps.savefig()

        nmax = max(con.max(), conb.max())

        plt.clf()
        I = coppstd
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 per-pixel std')
        ps.savefig()


    return (coimg,  coinvvar,  coppstd,  con,
            coimgb, coinvvarb, coppstdb, conb,
            masks, cube, sky,
            coadd.comin, coadd.comax, coadd.cominb, coadd.comaxb)


def estimate_sky(img, lo, hi, omit=None, maxdev=0., return_fit=False):
    # Estimate sky level by: compute the histogram within [lo,hi], fit
    # a parabola to the log-counts, return the argmax of that parabola.
    binedges = np.linspace(lo, hi, 25)
    counts,e = np.histogram(img.ravel(), bins=binedges)
    bincenters = binedges[:-1] + (binedges[1]-binedges[0])/2.

    if omit is not None:
        # Omit the bin containing value 'omit'
        okI = np.logical_not((binedges[:-1] < omit) * (omit < binedges[1:]))
        bincenters = bincenters[okI]
        counts = counts[okI]

    b = np.log10(np.maximum(1, counts))

    if maxdev > 0:
        # log-deviation of a bin from the mean of its neighbors --
        de = (b[1:-1] - (b[:-2] + b[2:])/2)
        print 'Max deviation:', np.max(de)
        okI = np.append(np.append([True], (de < maxdev)), [True])
        bincenters = bincenters[okI]
        b = b[okI]

    xscale = 0.5 * (hi - lo)
    x0 = (hi + lo) / 2.
    x = (bincenters - x0) / xscale

    A = np.zeros((len(x), 3))
    A[:,0] = 1.
    A[:,1] = x
    A[:,2] = x**2
    res = np.linalg.lstsq(A, b)
    X = res[0]
    mx = -X[1] / (2. * X[2])
    mx = (mx * xscale) + x0

    if return_fit:
        bfit = X[0] + X[1] * x + X[2] * x**2
        return (x * xscale + x0, b, bfit, mx)

    return mx


def estimate_sky_2(img, lo=None, hi=None, plo=1, phi=70, bins1=30,
                   flo=0.5, fhi=0.8, bins2=30,
                   return_fit=False):
    # Estimate sky level by: compute the histogram within [lo,hi], fit
    # a parabola to the log-counts, return the argmax of that parabola.
    # Coarse bin to find the peak (mode)
    if lo is None:
        lo = np.percentile(img, plo)
    if hi is None:
        hi = np.percentile(img, phi)

    binedges1 = np.linspace(lo, hi, bins1+1)
    counts1,e = np.histogram(img.ravel(), bins=binedges1)
    bincenters1 = binedges1[:-1] + (binedges1[1]-binedges1[0])/2.
    maxbin = np.argmax(counts1)
    maxcount = counts1[maxbin]
    mode = bincenters1[maxbin]

    # Search for bin containing < {flo,fhi} * maxcount
    ilo = maxbin
    while ilo > 0:
        ilo -= 1
        if counts1[ilo] < flo*maxcount:
            break
    ihi = maxbin
    while ihi < bins1-1:
        ihi += 1
        if counts1[ihi] < fhi*maxcount:
            break
    
    lo = bincenters1[ilo]
    hi = bincenters1[ihi]
    
    binedges = np.linspace(lo, hi, bins2)
    counts,e = np.histogram(img.ravel(), bins=binedges)
    bincenters = binedges[:-1] + (binedges[1]-binedges[0])/2.
    
    b = np.log10(np.maximum(1, counts))

    xscale = 0.5 * (hi - lo)
    x0 = (hi + lo) / 2.
    x = (bincenters - x0) / xscale

    A = np.zeros((len(x), 3))
    A[:,0] = 1.
    A[:,1] = x
    A[:,2] = x**2
    res = np.linalg.lstsq(A, b)
    X = res[0]
    mx = -X[1] / (2. * X[2])
    mx = (mx * xscale) + x0

    warn = False
    if not (mx > lo and mx < hi):
        print 'WARNING: sky estimate not bracketed by peak: lo %f, sky %f, hi %f' % (lo, mx, hi)
        warn = True
        
    if return_fit:
        bfit = X[0] + X[1] * x + X[2] * x**2
        return (x * xscale + x0, b, bfit, mx, warn, binedges1,counts1)
                

    return mx


def _coadd_one_round1((i, N, wise, table, L, ps, band, cowcs, medfilt,
                       do_check_md5, zp_lookup_obj)):
    '''
    For multiprocessing, the function called to do round 1 on a single
    input frame.
    '''
    t00 = Time()
    print
    print 'Coadd round 1, image', (i+1), 'of', N
    intfn = wise.intfn
    uncfn = intfn.replace('-int-', '-unc-')
    if unc_gz and (not int_gz):
        uncfn = uncfn + '.gz'
    maskfn = intfn.replace('-int-', '-msk-')
    if mask_gz and (not int_gz):
        maskfn = maskfn + '.gz'
    print 'intfn', intfn
    print 'uncfn', uncfn
    print 'maskfn', maskfn

    wcs = wise.wcs
    x0,x1,y0,y1 = wise.imextent
    wcs = wcs.get_subimage(int(x0), int(y0), int(1+x1-x0), int(1+y1-y0))
    slc = (slice(y0,y1+1), slice(x0,x1+1))

    cox0,cox1,coy0,coy1 = wise.coextent
    coW = int(1 + cox1 - cox0)
    coH = int(1 + coy1 - coy0)

    if do_check_md5:
        if not check_one_md5(wise):
            raise RuntimeError('MD5 check failed for ' + intfn + ' or unc/msk')

    # We read the full images for sky-estimation purposes -- really necessary?
    fullimg,ihdr = fitsio.read(intfn, header=True)
    fullmask = fitsio.read(maskfn)
    fullunc  = fitsio.read(uncfn )
    img  = fullimg [slc]
    mask = fullmask[slc]
    unc  = fullunc [slc]

    if not use_zp_meta:
        zp = zp_lookup_obj.get_zp(ihdr['MJD_OBS'])
    else:
        zp = ihdr['MAGZP']

    zpscale = 1. / zeropointToScale(zp)
    print 'Zeropoint:', zp, '-> scale', zpscale

    if band == 4:
        # In W4, the WISE single-exposure images are binned down
        # 2x2, so we are effectively splitting each pixel into 4
        # sub-pixels.  Spread out the flux.
        zpscale *= 0.25

    badbits = [0,1,2,3,4,5,6,7, 9, 
               10,11,12,13,14,15,16,17,18,
               21,26,27,28]
    if wise.phase == 3:
        # 3-band cryo phase:
        ## 19 pixel is "hard-saturated"
        ## 23 for W3 only: static-split droop residual present
        badbits.append(19)
        if band == 3:
            badbits.append(23)

    maskbits = sum([1<<bit for bit in badbits])
    goodmask = ((mask & maskbits) == 0)
    goodmask[unc == 0] = False
    goodmask[np.logical_not(np.isfinite(img))] = False
    goodmask[np.logical_not(np.isfinite(unc))] = False

    sig1 = median_f(unc[goodmask])
    print 'sig1:', sig1
    del mask
    del unc

    # our return value (quack):
    rr = Duck()
    # Patch masked pixels so we can interpolate
    rr.npatched = np.count_nonzero(np.logical_not(goodmask))
    print 'Pixels to patch:', rr.npatched
    # Many of the post-cryo frames have ~160,000 masked!
    if rr.npatched > 200000:
        print 'WARNING: too many pixels to patch:', rr.npatched
        return None
    ok = patch_image(img, goodmask.copy())
    if not ok:
        print 'WARNING: Patching failed:'
        print 'Image size:', img.shape
        print 'Number to patch:', rr.npatched
        return None
    assert(np.all(np.isfinite(img)))

    # Estimate sky level
    fullok = ((fullmask & maskbits) == 0)
    fullok[fullunc == 0] = False
    fullok[np.logical_not(np.isfinite(fullimg))] = False
    fullok[np.logical_not(np.isfinite(fullunc))] = False

    if medfilt:
        tmf0 = Time()
        mf = np.zeros_like(fullimg)
        ok = median_smooth(fullimg, np.logical_not(fullok), int(medfilt), mf)
        fullimg -= mf
        img = fullimg[slc]
        print 'Median filtering with box size', medfilt, 'took', Time()-tmf0
        if ps:
            # save for later...
            rr.medfilt = mf * zpscale
        del mf
        
    # add some noise to smooth out "dynacal" artifacts
    fim = fullimg[fullok]
    scan_frame_int = int_from_scan_frame(wise.scan_id, wise.frame_num)
    np.random.seed(scan_frame_int)
    fim += np.random.normal(scale=sig1, size=fim.shape) 
    if ps:
        vals,counts,fitcounts,sky,warn,be1,bc1 = estimate_mode(fim, return_fit=True)
        rr.hist = np.histogram(fullimg[fullok], range=(vals[0],vals[-1]), bins=100)
        rr.skyest = sky
        rr.skyfit = (vals, counts, fitcounts)
        
        if warn:
            # Background estimation plot
            plt.clf()

            # first-round histogram
            ee1 = be1.repeat(2)[1:-1]
            nn1 = bc1.repeat(2)
            plt.plot(ee1, nn1, 'b-', alpha=0.5)

            # full-image histogram
            n,e = rr.hist
            ee = e.repeat(2)[1:-1]
            nn = n.repeat(2)
            plt.plot(ee, nn, 'm-', alpha=0.5)

            # extended range
            n,e = np.histogram(fim, range=(np.percentile(fim, 1),
                                           np.percentile(fim, 90)), bins=100)
            ee = e.repeat(2)[1:-1]
            nn = n.repeat(2)
            plt.plot(ee, nn, 'g-', alpha=0.5)

            plt.twinx()
            plt.plot(vals, counts, 'm-', alpha=0.5)
            plt.plot(vals, fitcounts, 'r-', alpha=0.5)
            plt.axvline(sky, color='r')
            plt.title('%s %i' % (wise.scan_id, wise.frame_num))
            ps.savefig()

            plt.xlim(ee1[0], ee1[-1])
            ps.savefig()
            

    else:
        sky = estimate_mode(fim)

    print 'Estimated sky:', sky
    print 'Image median:', np.median(fullimg[fullok])
    print 'Image median w/ noise:', np.median(fim)

    del fim
    del fullunc
    del fullok
    del fullimg
    del fullmask

    # Convert to nanomaggies
    img -= sky
    img  *= zpscale
    sig1 *= zpscale

    # coadd subimage
    cosubwcs = cowcs.get_subimage(int(cox0), int(coy0), coW, coH)
    try:
        Yo,Xo,Yi,Xi,rims = resample_with_wcs(cosubwcs, wcs, [img], L,
                                             table=table)
    except OverlapError:
        print 'No overlap; skipping'
        return None
    rim = rims[0]
    assert(np.all(np.isfinite(rim)))
    print 'Pixels in range:', len(Yo)

    if ps:
        # save for later...
        rr.img = img
        
        if medfilt:
            print 'Median filter: rr.medfilt range', rr.medfilt.min(), rr.medfilt.max()
            print 'Sky:', sky*zpscale
            med = median_f(rr.medfilt.astype(np.float32).ravel())
            rr.rmedfilt = np.zeros((coH,coW), img.dtype)
            rr.rmedfilt[Yo,Xo] = (rr.medfilt[Yi, Xi].astype(img.dtype) - med)
            print 'rr.rmedfilt range', rr.rmedfilt.min(), rr.rmedfilt.max()

    # Scalar!
    rr.w = (1./sig1**2)
    rr.rimg = np.zeros((coH, coW), img.dtype)
    rr.rimg[Yo, Xo] = rim
    rr.rmask = np.zeros((coH, coW), np.uint8)
    # bit 0: old rmask
    # bit 1: old rmask2
    rr.rmask[Yo, Xo] = 1 + 2*goodmask[Yi, Xi]
    rr.wcs = wcs
    rr.sky = sky
    rr.zpscale = zpscale
    rr.zp = zp
    rr.ncopix = len(Yo)
    rr.coextent = wise.coextent
    rr.cosubwcs = cosubwcs

    if ps and medfilt and False:
        plt.clf()
        rows,cols = 2,2
        kwa = dict(interpolation='nearest', origin='lower',
                   vmin=-2.*sig1, vmax=3.*sig1, cmap='gray')

        mm = median_f(rr.medfilt.astype(np.float32))
        print 'Median medfilt:', 
        #mm = sky * zpscale
        print 'Sky*zpscale:', sky*zpscale
        
        origimg = rr.img + rr.medfilt - mm

        plt.subplot(rows, cols, 1)
        plt.imshow(binimg(origimg, 4), **kwa)
        plt.title('Image')
        plt.subplot(rows, cols, 2)
        plt.imshow(binimg(rr.medfilt - mm, 4), **kwa)
        plt.title('Median')
        plt.subplot(rows, cols, 3)
        plt.imshow(binimg(rr.img, 4), **kwa)
        plt.title('Image - Median')
        tag = ''
        if wise.moon_masked:
            tag += ' moon'
        plt.suptitle('%s %i%s' % (wise.scan_id, wise.frame_num, tag))
        ps.savefig()

    print Time() - t00
    return rr


def _coadd_wise_round1(cowcs, WISE, ps, band, table, L, tinyw, mp, medfilt,
                       checkmd5, bgmatch, cube1):
                       
    '''
    Do round-1 coadd.
    '''
    W = cowcs.get_width()
    H = cowcs.get_height()
    coimg   = np.zeros((H,W))
    coimgsq = np.zeros((H,W))
    cow     = np.zeros((H,W))

    zp_lookup_obj = ZPLookUp(band, poly=True)

    args = []
    for wi,wise in enumerate(WISE):
        args.append((wi, len(WISE), wise, table, L, ps, band, cowcs, medfilt,
                     checkmd5, zp_lookup_obj))
    rimgs = mp.map(_coadd_one_round1, args)
    del args

    print 'Accumulating first-round coadds...'
    cube = None
    if cube1:
        cube = np.zeros((len([rr for rr in rimgs if rr is not None]), H, W),
                        np.float32)
        z = 0
    t0 = Time()
    for wi,rr in enumerate(rimgs):
        if rr is None:
            continue
        cox0,cox1,coy0,coy1 = rr.coextent
        slc = slice(coy0,coy1+1), slice(cox0,cox1+1)

        if bgmatch:
            # Overlapping pixels:
            I = np.flatnonzero((cow[slc] > 0) * (rr.rmask&1 > 0))
            rr.bgmatch = 0.
            if len(I) > 0:
                bg = median_f(((coimg[slc].flat[I] / cow[slc].flat[I]) - 
                               rr.rimg.flat[I]).astype(np.float32))
                print 'Matched bg:', bg
                rr.rimg[(rr.rmask & 1) > 0] += bg
                rr.bgmatch = bg
                
        # note, rr.w is a scalar.
        coimgsq[slc] += rr.w * (rr.rimg**2)
        coimg  [slc] += rr.w *  rr.rimg
        cow    [slc] += rr.w * (rr.rmask & 1)

        if cube1:
            cube[(z,)+slc] = rr.rimg.astype(np.float32)
            z += 1
            
        # if ps:
        #     # Show the coadd as it's accumulated
        #     plt.clf()
        #     s1 = np.median([r2.w for r2 in rimgs if r2 is not None])
        #     s1 /= 5.
        #     plt.imshow(coimg / np.maximum(cow, tinyw), interpolation='nearest',
        #                origin='lower', vmin=-2.*s1, vmax=5.*s1)
        #     plt.title('%s %i' % (WISE.scan_id[wi], WISE.frame_num[wi]))
        #     ps.savefig()

    print Time()-t0

    coimg /= np.maximum(cow, tinyw)
    # Per-pixel std
    coppstd = np.sqrt(np.maximum(0, coimgsq / np.maximum(cow, tinyw)
                                 - coimg**2))

    if ps:
        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            n,e = rr.hist
            ee = e.repeat(2)[1:-1]
            nn = n.repeat(2)
            plt.plot(ee - rr.skyest, nn, 'b-', alpha=0.1)
        plt.xlabel('image - sky')
        ps.savefig()
        plt.yscale('log')
        ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            plt.plot(vals - rr.skyest, counts, 'b-', alpha=0.1)
            plt.plot(vals - rr.skyest, fitcounts, 'r-', alpha=0.1)
        plt.xlabel('image - sky')
        plt.title('sky hist vs fit')
        ps.savefig()

        plt.clf()
        o = 0
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            off = o * 0.01
            o += 1
            plt.plot(vals - rr.skyest, counts + off, 'b.-', alpha=0.1)
            plt.plot(vals - rr.skyest, fitcounts + off, 'r.-', alpha=0.1)
        plt.xlabel('image - sky')
        plt.title('sky hist vs fit')
        ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            plt.plot(vals - rr.skyest, counts - fitcounts, 'b-', alpha=0.1)
        plt.ylabel('log counts - log fit')
        plt.xlabel('image - sky')
        plt.title('sky hist fit residuals')
        ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            plt.plot(vals - rr.skyest, counts - fitcounts, 'b.', alpha=0.1)
        plt.ylabel('log counts - log fit')
        plt.xlabel('image - sky')
        plt.title('sky hist fit residuals')
        ps.savefig()

        ha = dict(range=(-8,8), bins=100, log=True, histtype='step')
        plt.clf()
        nn = []
        for rr in rimgs:
            if rr is None:
                continue
            mask = (rr.rmask & 1).astype(bool)
            rim = rr.rimg[mask]
            if len(rim) == 0:
                continue
            #n,b,p = plt.hist(rim, alpha=0.1, **ha)
            #nn.append((n,b))
            n,e = np.histogram(rim, range=ha['range'], bins=ha['bins'])
            lo = 3e-3
            nnn = np.maximum(3e-3, n/float(sum(n)))
            #print 'e', e
            #print 'nnn', nnn
            nn.append((nnn,e))
            plt.semilogy((e[:-1]+e[1:])/2., nnn, 'b-', alpha=0.1)
        plt.xlabel('rimg (-sky)')
        #yl,yh = plt.ylim()
        yl,yh = [np.percentile(np.hstack([n for n,e in nn]), p) for p in [3,97]]
        plt.ylim(yl, yh)
        ps.savefig()

        plt.clf()
        for n,b in nn:
            plt.semilogy((b[:-1] + b[1:])/2., n, 'b.', alpha=0.2)
        plt.xlabel('rimg (-sky)')
        plt.ylim(yl, yh)
        ps.savefig()

        plt.clf()
        n,b,p = plt.hist(coimg.ravel(), **ha)
        plt.xlabel('coimg')
        plt.ylim(max(1, min(n)), max(n)*1.1)
        ps.savefig()

    return rimgs, coimg, cow, coppstd, coimgsq, cube


def _bounce_one_coadd(A):
    try:
        return one_coadd(*A)
    except:
        import traceback
        print 'one_coadd failed:'
        traceback.print_exc()
        return -1

def todo(T, W, H, pixscale, outdirs, r0,r1,d0,d1, ps, dataset, bands=[1,2,3,4],
         margin=1.05, allsky=False, pargs={}, justdir=False):
    # Check which tiles still need to be done.
    need = []
    for band in bands:
        tiles = []
        for i in range(len(T)):
            found = False
            for outdir in outdirs:
                thisdir = get_dir_for_coadd(outdir, T.coadd_id[i])
                if justdir:
                    ofn = thisdir
                else:
                    tag = 'unwise-%s-w%i' % (T.coadd_id[i], band)
                    prefix = os.path.join(thisdir, tag)
                    #ofn = prefix + '-img-m.fits'
                    ofn = prefix + '-frames.fits'

                if os.path.exists(ofn):
                    print 'Output file exists:', ofn
                    found = True
                    break
            if found:
                tiles.append(T.coadd_id[i])
                continue

            print 'Need', ofn
            need.append(band * arrayblock + i)

        fns = []
        if band == bands[0]:
            print 'plot A'
            plot_region(r0,r1,d0,d1, ps, T, None, fns, W, H, pixscale,
                        margin=margin, allsky=allsky, tiles=tiles, **pargs)
        else:
            print 'plot B'
            plot_region(r0,r1,d0,d1, ps, None, None, fns, W, H, pixscale,
                        margin=margin, allsky=allsky, tiles=tiles, **pargs)
    print ' '.join('%i' %i for i in need)

    # write out scripts
    if False:
        for i in need:
            script = '\n'.join(['#! /bin/bash',
                                ('#PBS -N %s-%i' % (dataset, i)),
                                '#PBS -l cput=1:00:00',
                                '#PBS -l pvmem=4gb',
                                'cd $PBS_O_WORKDIR',
                                ('export PBS_ARRAYID=%i' % i),
                                './wise-coadd.py',
                                ''])
            sfn = 'pbs-%s-%i.sh' % (dataset, i)
            write_file(script, sfn)
            os.system('chmod 755 %s' % sfn)

    # Collapse contiguous ranges
    strings = []
    if len(need):
        start = need.pop(0)
        end = start
        while len(need):
            x = need.pop(0)
            if x == end + 1:
                # extend this run
                end = x
            else:
                # run finished; output and start new one.
                if start == end:
                    strings.append('%i' % start)
                else:
                    strings.append('%i-%i' % (start, end))
                start = end = x
        # done; output
        if start == end:
            strings.append('%i' % start)
        else:
            strings.append('%i-%i' % (start, end))
        print ','.join(strings)
    else:
        print 'Done (party now)'


def get_wise_frames_for_dataset(dataset, r0,r1,d0,d1,
                                randomize=False, cache=True, dirnm=None):
    fn = '%s-frames.fits' % dataset
    if dirnm is not None:
        fn = os.path.join(dirnm, fn)
    if os.path.exists(fn) and cache:
        print 'Reading', fn
        WISE = fits_table(fn)
    else:
        WISE = get_wise_frames(r0,r1,d0,d1)
        # bool -> uint8 to avoid confusing fitsio
        WISE.moon_masked = WISE.moon_masked.astype(np.uint8)
        if randomize:
            print 'Randomizing frame order...'
            WISE.cut(np.random.permutation(len(WISE)))

        WISE.writeto(fn)
    # convert to boolean
    WISE.moon_masked = (WISE.moon_masked != 0)
    return WISE
   

def main():
    import optparse
    from astrometry.util.multiproc import multiproc

    parser = optparse.OptionParser('%prog [options]')
    parser.add_option('--threads', dest='threads', type=int, help='Multiproc',
                      default=None)
    parser.add_option('--threads1', dest='threads1', type=int, default=None,
                      help='Multithreading during round 1')

    parser.add_option('--todo', dest='todo', action='store_true',
                      default=False, help='Print and plot fields to-do')
    parser.add_option('--just-dir', dest='justdir', action='store_true',
                      default=False, help='With --todo, just check for directory, not image file')
                      
    parser.add_option('-w', dest='wishlist', action='store_true',
                      default=False, help='Print needed frames and exit?')
    parser.add_option('--plots', dest='plots', action='store_true',
                      default=False)
    parser.add_option('--plots2', dest='plots2', action='store_true',
                      default=False)
    parser.add_option('--pdf', dest='pdf', action='store_true', default=False)

    parser.add_option('--plot-prefix', dest='plotprefix', default=None)

    parser.add_option('--outdir', '-o', dest='outdir', default='unwise-coadds',
                      help='Output directory: default %default')
    parser.add_option('--outdir2', dest='outdir2',
                      help='Additional output directory')

    parser.add_option('--size', dest='size', default=2048, type=int,
                      help='Set output image size in pixels; default %default')
    parser.add_option('--width', dest='width', default=0, type=int,
                      help='Set output image width in pixels; default --size')
    parser.add_option('--height', dest='height', default=0, type=int,
                      help='Set output image height in pixels; default --size')

    parser.add_option('--pixscale', dest='pixscale', type=float, default=2.75,
                      help='Set coadd pixel scale, default %default arcsec/pixel')
    parser.add_option('--cube', dest='cube', action='store_true',
                      default=False, help='Save & write out image cube')
    parser.add_option('--cube1', dest='cube1', action='store_true',
                      default=False, help='Save & write out image cube for round 1')

    parser.add_option('--dataset', dest='dataset', default='sequels',
                      help='Dataset (region of sky) to coadd')

    parser.add_option('--frame0', dest='frame0', default=0, type=int,
                      help='Only use a subset of the frames: starting with frame0')
    parser.add_option('--nframes', dest='nframes', default=0, type=int,
                      help='Only use a subset of the frames: number nframes')

    parser.add_option('--medfilt', dest='medfilt', type=int, default=None,
                      help=('Median filter with a box twice this size (+1),'+
                            ' to remove varying background.  Default: none for W1,W2; 50 for W3,W4.'))

    parser.add_option('--force', dest='force', action='store_true',
                      default=False, 
                      help='Run even if output file already exists?')

    parser.add_option('--maxmem', dest='maxmem', type=float, default=0,
                      help='Quit if predicted memory usage > n GB')

    parser.add_option('--dsky', dest='dsky', action='store_true',
                      default=False,
                      help='Do background-matching by matching medians '
                      '(to first-round coadd)')

    parser.add_option('--bgmatch', dest='bgmatch', action='store_true',
                      default=False,
                      help='Do background-matching by matching medians '
                      '(when accumulating first-round coadd)')

    parser.add_option('--center', dest='center', action='store_true',
                      default=False,
                      help='Read frames in order of distance from center; for debugging.')

    parser.add_option('--minmax', action='store_true',
                      help='Record the minimum and maximum values encountered during coadd?')

    parser.add_option('--md5', dest='md5', action='store_true', default=False,
                      help='Check md5sums on all input files?')

    parser.add_option('--all-md5', dest='allmd5', action='store_true', default=False,
                      help='Check all md5sums and exit')

    parser.add_option('--ra', dest='ra', type=float, default=None,
                      help='Build coadd at given RA center')
    parser.add_option('--dec', dest='dec', type=float, default=None,
                      help='Build coadd at given Dec center')
    parser.add_option('--band', type=int, default=None, action='append',
                      help='with --ra,--dec: band(s) to do (1,2,3,4)')

    parser.add_option('--tile', dest='tile', type=str, default=None,
                      help='Run a single tile, eg, 0832p196')

    parser.add_option('--preprocess', dest='preprocess', action='store_true',
                      default=False, help='Preprocess (write *-atlas, *-frames.fits) only')

    parser.add_option('--rchi-fraction', dest='rchi_fraction', type=float,
                      default=0.01, help='Fraction of outlier pixels to reject frame')

    parser.add_option('--epoch', type=int, help='Keep only input frames in the given epoch, zero-indexed')

    # adding this default value of before is appropriate for first year NEOWISER processing
    # to avoid special non-public Hyades data
    parser.add_option('--before', type=float, help='Keep only input frames before the given MJD',
                      default=57058.9938976122)
    parser.add_option('--after',  type=float, help='Keep only input frames after the given MJD')

    parser.add_option('--int_gz', dest='int_gz', action='store_true', default=False,
                      help='Are L1b int images gzipped?')
    parser.add_option('--use_zp_meta', dest='use_zp_meta', action='store_true', default=False,
                      help='Should coadd use MAGZP metadata for zero points?')

    opt,args = parser.parse_args()

    global int_gz
    int_gz = opt.int_gz

    global use_zp_meta
    use_zp_meta = opt.use_zp_meta

    if opt.threads:
        mp2 = multiproc(opt.threads)
    else:
        mp2 = multiproc()
    if opt.threads1 is None:
        mp1 = mp2
    else:
        mp1 = multiproc(opt.threads1)

    batch = False
    arr = os.environ.get('PBS_ARRAYID')
    if arr is not None:
        arr = int(arr)
        batch = True

    radec = opt.ra is not None and opt.dec is not None

    if len(args) == 0 and arr is None and not (opt.todo or opt.allmd5 or radec or opt.tile or opt.preprocess):
        print 'No tile(s) specified'
        parser.print_help()
        sys.exit(-1)

    print 'unwise_coadd.py starting: args:', sys.argv
    print 'PBS_ARRAYID:', arr

    print 'opt:', opt
    print dir(opt)

    Time.add_measurement(MemMeas)

    W = H = opt.size
    if opt.width:
        W = opt.width
    if opt.height:
        H = opt.height

    dataset = opt.dataset

    randomize = False
    pmargin = 1.05
    pallsky = False
    plotargs = {}
    todoargs = {}

    if radec:
        dataset = ''
    if opt.tile is not None:
        dataset = ''

    if dataset == 'sequels':
        # SEQUELS
        r0,r1 = 120.0, 210.0
        d0,d1 =  45.0,  60.0
    elif dataset == 'gc':
        # Galactic center
        r0,r1 = 262.0, 270.0
        d0,d1 = -32.0, -26.0
    elif dataset == 'pupa':
        # Puppis A supernova remnant 1253m425
        r0,r1 = 125.1, 125.5
        d0,d1 = -42.6, -42.4
    elif dataset == 'swire':
        # Spitzer SWIRE
        r0,r1 = 157.0, 166.0
        #d0,d1 =  55.0,  60.0
        d0,d1 =  56.0,  59.0
    elif dataset == 'cosmos':
        r0,r1 = 149.61, 150.62
        d0,d1 =   1.66,   2.74
    elif dataset == 'w3':
        # CFHT LS W3
        r0,r1 = 210.593,  219.132
        d0,d1 =  51.1822,  54.1822
    elif dataset.startswith('s82'):
        # SDSS Stripe 82
        r0,r1 = 0., 360.
        d0,d1 = -1.5, 1.5
        pallsky = True
        rmap = dict(s82a=(0.,90.), s82b=(90.,180.), s82c=(180.,270.), s82d=(270.,360.))
        r0,r1 = rmap.get(dataset, (r0,r1))
    elif dataset == 's82':
        # SDSS Stripe 82
        r0,r1 = 0., 360.
        d0,d1 = -1.5, 1.5
        pallsky = True
    elif dataset == 'm31':
        r0,r1 =  9.0, 12.5
        d0,d1 = 40.5, 42.5
    elif dataset in ['npole', 'npole-rand']:
        # North ecliptic pole
        # (270.0, 66.56)
        r0,r1 = 265.0, 275.0
        d0,d1 =  64.6,  68.6
        if dataset == 'npole-rand':
            randomize = True
        pmargin = 1.5
    elif dataset in ['sdss', 'ngc', 'sgca', 'sgcb']:
        if dataset == 'sdss':
            # SDSS -- whole footprint
            r0,r1 =   0., 360.
            d0,d1 = -30.,  90.
        # SDSS -- approximate NGC/SGC bounding boxes
        elif dataset == 'ngc':
            r0,r1 = 130., 230.
            d0,d1 =   5.,  45.
        elif dataset == 'sgca':
            r0,r1 = 330., 360.
            d0,d1 =  -5.,  20.
        elif dataset == 'sgcb':
            r0,r1 =   0.,  20.
            d0,d1 =  -5.,  20.
        
        pallsky = True
        plotargs.update(label_tiles=False, draw_outline=False)
        plotargs.update(ra=180., dec=0.)
        plotargs.update(grid_spacing=[30,30,30,30])
        todoargs.update(bands=[1,2,4])

    elif dataset == 'allsky':
        r0,r1 =   0., 360.
        d0,d1 = -90.,  90.
        pallsky = True
        plotargs.update(label_tiles=False, draw_outline=False)
        plotargs.update(ra=180., dec=0.)
        plotargs.update(grid_spacing=[30,30,30,30])
        todoargs.update(bands=[1,2,3,4])

    elif dataset in ['allnorth', 'allsouth']:
        global arrayblock
        arrayblock = 10000
        
        r0,r1 =  0., 360.
        if dataset == 'allnorth':
            d0,d1 =  0.,  90.
        else:
            d0,d1 = -90.,  0.
        pallsky = True
        plotargs.update(label_tiles=False, draw_outline=False)
        plotargs.update(ra=180., dec=0.)
        plotargs.update(grid_spacing=[30,30,30,30])
        todoargs.update(bands=[3,4])


    elif dataset == 'examples':
        pass

    elif dataset == 'deepqso':
        r0,r1 =  36.0, 42.0
        d0,d1 =  -1.3, 1.3
        pmargin = 1.5
        plotargs.update(grid_spacing=(1,1,2,2))

    elif dataset == 'three':
        fn = '%s-atlas.fits' % dataset
        if not os.path.exists(fn):
            T = fits_table('allsky-atlas.fits')
            l,b = radectoecliptic(T.ra, T.dec)
            I = np.flatnonzero((l > 220) * (l < 230) * (b > 0) * (b < 75))
            T.cut(I)
            print 'Cut to', len(T), 'tiles in L,B slice'
            T.writeto(fn)
        else:
            T = fits_table(fn)
        r0 = T.ra.min()
        r1 = T.ra.max()
        d0 = T.dec.min()
        d1 = T.dec.max()

    else:
        if radec:
            dataset = ('custom-%04i%s%03i' % (int(opt.ra*10.),
                                              'p' if opt.dec >= 0. else 'm',
                                              int(np.abs(opt.dec)*10.)))
            print 'Setting custom dataset', dataset
            cosd = np.cos(np.deg2rad(opt.dec))
            r0 = opt.ra - (opt.pixscale * W/2.)/3600. / cosd
            r1 = opt.ra + (opt.pixscale * W/2.)/3600. / cosd
            d0 = opt.dec - (opt.pixscale * H/2.)/3600.
            d1 = opt.dec + (opt.pixscale * H/2.)/3600.

        elif opt.tile is not None:
            # parse it
            if len(opt.tile) != 8:
                print '--tile expects string like RRRR[pm]DDD'
                sys.exit(-1)
            ra,dec = tile_to_radec(opt.tile)
            print 'Tile RA,Dec', ra,dec
            dataset = opt.tile
            r0 = ra  - 0.001
            r1 = ra  + 0.001
            d0 = dec - 0.001
            d1 = dec + 0.001

        else:
            assert(False)

    tiles = []
    

    if radec:
        T = fits_table()
        T.coadd_id = np.array([dataset])
        T.ra = np.array([opt.ra])
        T.dec = np.array([opt.dec])
        if len(args) == 0:
            if len(opt.band):
                tiles.extend([b * arrayblock for b in opt.band])
            else:
                tiles.append(arrayblock)
    else:
        fn = '%s-atlas.fits' % dataset
        print 'Looking for file', fn
        if os.path.exists(fn):
            print 'Reading', fn
            T = fits_table(fn)
        else:
            T = get_atlas_tiles(r0,r1,d0,d1, W,H, opt.pixscale)
            T.writeto(fn)
            print 'Wrote', fn

        if not len(args):
            tiles.append(arr)


    if opt.plotprefix is None:
        opt.plotprefix = dataset
    ps = PlotSequence(opt.plotprefix, format='%03i')
    if opt.pdf:
        ps.suffixes = ['png','pdf']

    if opt.todo:
        odirs = [opt.outdir]
        if opt.outdir2 is not None:
            odirs.append(opt.outdir2)
        todo(T, W, H, opt.pixscale, odirs, r0,r1,d0,d1, ps, dataset,
             margin=pmargin, allsky=pallsky, pargs=plotargs, justdir=opt.justdir,
             **todoargs)
        return 0

    if not opt.plots:
        ps = None

    WISE = get_wise_frames_for_dataset(dataset, r0,r1,d0,d1)

    if opt.allmd5:
        Ibad = check_md5s(WISE)
        print 'Found', len(Ibad), 'bad MD5s'
        for i in Ibad:
            intfn = get_l1b_file(wisedir, WISE.scan_id[i], WISE.frame_num[i], WISE.band[i])
            print ('(wget -r -N -nH -np -nv --cut-dirs=4 -A "*w%i*" "http://irsa.ipac.caltech.edu/ibe/data/wise/merge/merge_p1bm_frm/%s")' %
                   (WISE.band[i], os.path.dirname(intfn).replace(wisedir + '/', '')))
        sys.exit(0)

    if not os.path.exists(opt.outdir) and not opt.wishlist:
        print 'Creating output directory', opt.outdir
        try:
            os.makedirs(opt.outdir)
        except:
            # if we just lost the race condition, fine
            if not os.path.exists(opt.outdir):
                print 'os.makedirs failed, and', opt.outdir, 'does not exist'
                raise

    if opt.preprocess:
        print 'Preprocessing done'
        sys.exit(0)

    for a in args:
        # parse "qsub -t" format: n,n1-n2,n3
        for term in a.split(','):
            if '-' in term:
                aa = term.split('-')
                if len(aa) != 2:
                    print 'With arg containing a dash, expect two parts'
                    print aa
                    sys.exit(-1)
                start = int(aa[0])
                end = int(aa[1])
                for i in range(start, end+1):
                    tiles.append(i)
            else:
                tiles.append(int(term))

    for tileid in tiles:
        band   = (opt.band)[0]
        tileid = tileid % arrayblock
        assert(tileid < len(T))
        print 'Doing coadd tile', T.coadd_id[tileid], 'band', band
        t0 = Time()

        medfilt = opt.medfilt
        if medfilt is None:
            if band in [3,4]:
                medfilt = 50
            else:
                medfilt = 0

        if one_coadd(T[tileid], band, W, H, opt.pixscale, WISE, ps,
                     opt.wishlist, opt.outdir, mp1, mp2,
                     opt.cube, opt.plots2, opt.frame0, opt.nframes, opt.force,
                     medfilt, opt.maxmem, opt.dsky, opt.md5, opt.bgmatch,
                     opt.center, opt.minmax, opt.rchi_fraction, opt.cube1,
                     opt.epoch, opt.before, opt.after):
            return -1
        print 'Tile', T.coadd_id[tileid], 'band', band, 'took:', Time()-t0
    return 0

if __name__ == '__main__':
    sys.exit(main())

