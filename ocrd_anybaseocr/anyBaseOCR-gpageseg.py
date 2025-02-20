#!/usr/bin/python

# TODO:
# ! add option for padding
# - fix occasionally missing page numbers
# - treat large h-whitespace as separator
# - handle overlapping candidates
# - use cc distance statistics instead of character scale
# - page frame detection
# - read and use text image segmentation mask
# - pick up stragglers
# ? laplacian as well

from pylab import *
import argparse
import glob
import os
import os.path
import traceback
from scipy.ndimage import measurements
#from scipy.misc import imsave
import imageio
from scipy.ndimage.filters import gaussian_filter, uniform_filter, maximum_filter
from multiprocessing import Pool
import ocrolib
from ocrolib import psegutils, morph, sl
from ocrolib.toplevel import *

parser = argparse.ArgumentParser()
# error checking
parser.add_argument('-n', '--nocheck', action="store_true",
                    help="disable error checking on inputs")

parser.add_argument('-z', '--zoom', type=float, default=0.5,
                    help='zoom for page background estimation, smaller=faster')

parser.add_argument('--gray', action='store_true',
                    help='output grayscale lines as well (%(default)s)')
parser.add_argument('-q', '--quiet', action='store_true',
                    help='be less verbose (%(default)s)')

# limits
parser.add_argument('--minscale', type=float, default=8.0,
                    help='minimum scale permitted (%(default)s)')  # default was 12.0, Mohsin with Ajraf and Saqib chnaged it into 8.0
parser.add_argument('--maxlines', type=float, default=300,
                    help='maximum # lines permitted (%(default)s)')

# scale parameters
parser.add_argument('--scale', type=float, default=0.0,
                    help='the basic scale of the document (roughly, xheight) 0=automatic (%(default)s)')
parser.add_argument('--hscale', type=float, default=1.0,
                    help='non-standard scaling of horizontal parameters (%(default)s)')
parser.add_argument('--vscale', type=float, default=1.7,
                    help='non-standard scaling of vertical parameters (%(default)s)')

# line parameters
parser.add_argument('--threshold', type=float, default=0.2,
                    help='baseline threshold (%(default)s)')
parser.add_argument('--noise', type=int, default=8,
                    help="noise threshold for removing small components from lines (%(default)s)")
parser.add_argument('--usegauss', action='store_true',
                    help='use gaussian instead of uniform (%(default)s)')

# column parameters
parser.add_argument('--maxseps', type=int, default=2,
                    help='maximum black column separators (%(default)s)')
parser.add_argument('--sepwiden', type=int, default=10,
                    help='widen black separators (to account for warping) (%(default)s)')
parser.add_argument('-b', '--blackseps', action="store_true",
                    help="also check for black column separators")

# whitespace column separators
parser.add_argument('--maxcolseps', type=int, default=2,
                    help='maximum # whitespace column separators (%(default)s)')
parser.add_argument('--csminaspect', type=float, default=1.1,
                    help='minimum aspect ratio for column separators')
parser.add_argument('--csminheight', type=float, default=6.5,
                    help='minimum column height (units=scale) (%(default)s)')

# wait for input after everything is done

parser.add_argument('-p', '--pad', type=int, default=3,
                    help='padding for extracted lines (%(default)s)')
parser.add_argument('-e', '--expand', type=int, default=3,
                    help='expand mask for grayscale extraction (%(default)s)')
parser.add_argument('-Q', '--parallel', type=int, default=0,
                    help="number of CPUs to use")
parser.add_argument('-d', '--debug', action="store_true")
parser.add_argument('files', nargs='+')

args = parser.parse_args()
args.files = ocrolib.glob_all(args.files)


def norm_max(v):
    return v/amax(v)


def check_page(image):
    if len(image.shape) == 3:
        return "input image is color image %s" % (image.shape,)
    if mean(image) < median(image):
        return "image may be inverted"
    h, w = image.shape
    if h < 600:
        return "image not tall enough for a page image %s" % (image.shape,)
    if h > 10000:
        return "image too tall for a page image %s" % (image.shape,)
    if w < 600:
        return "image too narrow for a page image %s" % (image.shape,)
    if w > 10000:
        return "line too wide for a page image %s" % (image.shape,)
    slots = int(w*h*1.0/(30*30))
    _, ncomps = measurements.label(image > mean(image))
    if ncomps < 10:
        return "too few connected components for a page image (got %d)" % (ncomps,)
    if ncomps > slots:
        return "too many connnected components for a page image (%d > %d)" % (ncomps, slots)
    return None


if len(args.files) < 1:
    parser.print_help()
    sys.exit(0)

print()
print("#"*10, (" ".join(sys.argv))[:60])
print()

if args.parallel > 1:
    args.quiet = 1


def B(a):
    if a.dtype == dtype('B'):
        return a
    return array(a, 'B')


def DSAVE(title, image):
    if not args.debug:
        return
    if type(image) == list:
        assert len(image) == 3
        image = transpose(array(image), [1, 2, 0])
    fname = "_"+title+".png"
    print("debug", fname)
    imageio.imwrite(fname, image)


################################################################
# Column finding.
###
# This attempts to find column separators, either as extended
# vertical black lines or extended vertical whitespace.
# It will work fairly well in simple cases, but for unusual
# documents, you need to tune the parameters.
################################################################

def compute_separators_morph(binary, scale):
    """Finds vertical black lines corresponding to column separators."""
    d0 = int(max(5, scale/4))
    d1 = int(max(5, scale))+args.sepwiden
    thick = morph.r_dilation(binary, (d0, d1))
    vert = morph.rb_opening(thick, (10*scale, 1))
    vert = morph.r_erosion(vert, (d0//2, args.sepwiden))
    vert = morph.select_regions(vert, sl.dim1, min=3, nbest=2*args.maxseps)
    vert = morph.select_regions(vert, sl.dim0, min=20*scale, nbest=args.maxseps)
    return vert


def compute_colseps_morph(binary, scale, maxseps=3, minheight=20, maxwidth=5):
    """Finds extended vertical whitespace corresponding to column separators
    using morphological operations."""
    boxmap = psegutils.compute_boxmap(binary, scale, (0.4, 5), dtype='B')
    bounds = morph.rb_closing(B(boxmap), (int(5*scale), int(5*scale)))
    bounds = maximum(B(1-bounds), B(boxmap))
    cols = 1-morph.rb_closing(boxmap, (int(20*scale), int(scale)))
    cols = morph.select_regions(cols, sl.aspect, min=args.csminaspect)
    cols = morph.select_regions(cols, sl.dim0, min=args.csminheight*scale, nbest=args.maxcolseps)
    cols = morph.r_erosion(cols, (int(0.5+scale), 0))
    cols = morph.r_dilation(cols, (int(0.5+scale), 0), origin=(int(scale/2)-1, 0))
    return cols


def compute_colseps_mconv(binary, scale=1.0):
    """Find column separators using a combination of morphological
    operations and convolution."""
    h, w = binary.shape
    smoothed = gaussian_filter(1.0*binary, (scale, scale*0.5))
    smoothed = uniform_filter(smoothed, (5.0*scale, 1))
    thresh = (smoothed < amax(smoothed)*0.1)
    DSAVE("1thresh", thresh)
    blocks = morph.rb_closing(binary, (int(4*scale), int(4*scale)))
    DSAVE("2blocks", blocks)
    seps = minimum(blocks, thresh)
    seps = morph.select_regions(seps, sl.dim0, min=args.csminheight*scale, nbest=args.maxcolseps)
    DSAVE("3seps", seps)
    blocks = morph.r_dilation(blocks, (5, 5))
    DSAVE("4blocks", blocks)
    seps = maximum(seps, 1-blocks)
    DSAVE("5combo", seps)
    return seps


def compute_colseps_conv(binary, scale=1.0):
    """Find column separators by convoluation and
    thresholding."""
    h, w = binary.shape
    # find vertical whitespace by thresholding
    smoothed = gaussian_filter(1.0*binary, (scale, scale*0.5))
    smoothed = uniform_filter(smoothed, (5.0*scale, 1))
    thresh = (smoothed < amax(smoothed)*0.1)
    ####imsave('/home/gupta/Documents/1_thresh.png', thresh)
    # DSAVE("1thresh",thresh)
    # find column edges by filtering

#
    grad = gaussian_filter(1.0*binary, (scale, scale*0.5), order=(0, 1))
    grad = uniform_filter(grad, (10.0*scale, 1))
    # grad = abs(grad) # use this for finding both edges
    grad = (grad > 0.25*amax(grad))
    grad1 = morph.select_regions(grad, sl.dim0, min=args.csminheight*scale, nbest=args.maxcolseps+10)

    ####imsave('/home/gupta/Documents/2_grad.png', grad1)
    x = (1-thresh)*(1-grad1)
    thresh11 = (1-thresh)*x
    ####imsave('/home/gupta/Documents/3_x.png', thresh11)

    #############################################################################################################
    for r in range(0, len(thresh11)):
        count = 0
        for c in range(0, len(thresh11[0])):
            if(thresh11[r][c] == 1):
                continue
            count += 1
            if(c != len(thresh11[0])-1 and thresh11[r][c+1] == 1):
                if(count <= 50):
                    for z in range(c-count, c+1):
                        thresh11[r][z] = 1
                count = 0

    y = 1-(thresh11*(1-thresh))
    ####imsave('/home/gupta/Documents/4_uniformed.png', y)

    #############################################################################################################

    # DSAVE("2grad",grad)
    # combine edges and whitespace
    seps = minimum(thresh, maximum_filter(grad, (int(scale), int(5*scale))))
    seps = maximum_filter(seps, (int(2*scale), 1))
#
    ####imsave('/home/gupta/Documents/5_seps.png', seps)
    h, w = seps.shape
    smoothed = gaussian_filter(1.0*seps, (scale, scale*0.5))
    smoothed = uniform_filter(smoothed, (5.0*scale, 1))
    seps1 = (smoothed < amax(smoothed)*0.1)
    ####imsave('/home/gupta/Documents/6_smooth.png', seps1)
    seps1 = 1-seps1
#
    ####imsave('/home/gupta/Documents/7_smooth.png', seps1)
    seps1 = (grad)*seps1
    ####imsave('/home/gupta/Documents/8_multigrad.png', seps1)

    #############################################################################################################
    for c in range(0, len(seps1[0])):
        count = 0
        for r in range(0, len(seps1)):
            if(seps1[r][c] == 1):
                continue
            count += 1
            if(r != len(seps1)-1 and seps1[r+1][c] == 1):
                if(count <= 400):  # by making it 300 u can improve
                    for z in range(r-count, r+1):
                        seps1[z][c] = 1
                count = 0

    ####imsave('/home/gupta/Documents/9_uniformed.png', seps1)
    #############################################################################################################

    seps1 = morph.select_regions(seps1, sl.dim0, min=args.csminheight*scale, nbest=args.maxcolseps+10)
    ####imsave('/home/gupta/Documents/10_seps1.png', seps1)
#
    # seps2=seps1*y
    # t=seps1*(1-y)
    ####imsave('/home/gupta/Documents/t.png', t)
    ####imsave('/home/gupta/Documents/s.png', seps2)

#
    seps1 = (seps1*(1-y))+seps1
    for c in range(0, len(seps1[0])):
        for r in range(0, len(seps1)):
            if(seps1[r][c] != 0):
                seps1[r][c] = 1
    ####imsave('/home/gupta/Documents/11_testing.png', 0.7*seps1+0.3*binary)
    # f=(seps1-seps2)+seps1

    #############################################################################################################
    for c in range(0, len(seps1[0])):
        count = 0
        for r in range(0, len(seps1)):
            if(seps1[r][c] == 1):
                continue
            count += 1
            if(r != len(seps1)-1 and seps1[r+1][c] == 1):
                if(count <= 350):
                    for z in range(r-count, r+1):
                        seps1[z][c] = 1
                count = 0

    ####imsave('/home/gupta/Documents/12_uniformed.png', seps1)
    #############################################################################################################

    ####imsave('/home/gupta/Documents/13_col_sep.png', seps1)
    return seps1


def compute_colseps(binary, scale):
    """Computes column separators either from vertical black lines or whitespace."""
    colseps = compute_colseps_conv(binary, scale)
    ####imsave('/home/gupta/Documents/colwsseps.png', 0.7*colseps+0.3*binary)
    # DSAVE("colwsseps",0.7*colseps+0.3*binary)
    if args.blackseps:
        seps = compute_separators_morph(binary, scale)
        ####imsave('/home/gupta/Documents/colseps.png', 0.7*seps+0.3*binary)
        # DSAVE("colseps",0.7*seps+0.3*binary)
        #colseps = compute_colseps_morph(binary,scale)
        colseps = maximum(colseps, seps)
        binary = minimum(binary, 1-seps)
    return colseps, binary


################################################################
# Text Line Finding.
###
# This identifies the tops and bottoms of text lines by
# computing gradients and performing some adaptive thresholding.
# Those components are then used as seeds for the text lines.
################################################################

def compute_gradmaps(binary, scale):
    # use gradient filtering to find baselines
    boxmap = psegutils.compute_boxmap(binary, scale, (0.4, 5))
    cleaned = boxmap*binary
    ####imsave('/home/gupta/Documents/cleaned.png', cleaned)
    ####imsave('/home/gupta/Documents/boxmap.png', boxmap)
    # DSAVE("cleaned",cleaned)
    if args.usegauss:
        # this uses Gaussians
        grad = gaussian_filter(1.0*cleaned, (args.vscale*0.3*scale,
                                             args.hscale*6*scale), order=(1, 0))
    else:
        # this uses non-Gaussian oriented filters
        grad = gaussian_filter(1.0*cleaned, (max(4, args.vscale*0.3*scale),
                                             args.hscale*scale), order=(1, 0))
        grad = uniform_filter(grad, (args.vscale, args.hscale*6*scale))
    bottom = ocrolib.norm_max((grad < 0)*(-grad))
    top = ocrolib.norm_max((grad > 0)*grad)
    testseeds = zeros(binary.shape, 'i')
    ####imsave('/home/gupta/Documents/grad.png', grad)
    ####imsave('/home/gupta/Documents/top.png', [testseeds,1.0*top,binary])
    ####imsave('/home/gupta/Documents/bottom.png', [testseeds,1.0*bottom,binary])
    return bottom, top, boxmap


def compute_line_seeds(binary, bottom, top, colseps, scale):
    """Base on gradient maps, computes candidates for baselines
    and xheights.  Then, it marks the regions between the two
    as a line seed."""
    t = args.threshold  # 0.2###############################################################more focus here for bigger fonts.!!!
    # print "SbU", t
    vrange = int(args.vscale*scale)
    bmarked = maximum_filter(bottom == maximum_filter(bottom, (vrange, 0)), (2, 2))
    bmarked *= array((bottom > t*amax(bottom)*t)*(1-colseps), dtype=bool)
    tmarked = maximum_filter(top == maximum_filter(top, (vrange, 0)), (2, 2))
    tmarked *= array((top > t*amax(top)*t/2)*(1-colseps), dtype=bool)
    tmarked = maximum_filter(tmarked, (1, 20))
    testseeds = zeros(binary.shape, 'i')
    seeds = zeros(binary.shape, 'i')
    delta = max(3, int(scale/2))
    for x in range(bmarked.shape[1]):
        transitions = sorted([(y, 1) for y in psegutils.find(bmarked[:, x])]+[(y, 0) for y in psegutils.find(tmarked[:, x])])[::-1]
        transitions += [(0, 0)]
        for l in range(len(transitions)-1):
            y0, s0 = transitions[l]
            if s0 == 0:
                continue
            seeds[y0-delta:y0, x] = 1
            y1, s1 = transitions[l+1]
            if s1 == 0 and (y0-y1) < 5*scale:
                seeds[y1:y0, x] = 1
    seeds = maximum_filter(seeds, (1, int(1+scale)))
    seeds *= (1-colseps)
    ###
    ####imsave('/home/gupta/Documents/seeds.png', seeds)
    ####imsave('/home/gupta/Documents/top_bottom.png', [testseeds,0.3*tmarked+0.7*bmarked,binary])
    ###
    ####imsave('/home/gupta/Documents/lineseeds.png', [seeds,0.3*tmarked+0.7*bmarked,binary])
    # DSAVE("lineseeds",[seeds,0.3*tmarked+0.7*bmarked,binary])
    seeds, _ = morph.label(seeds)
    return seeds


################################################################
# The complete line segmentation process.
################################################################

def remove_hlines(binary, scale, maxsize=10):
    labels, _ = morph.label(binary)
    objects = morph.find_objects(labels)
    for i, b in enumerate(objects):
        if sl.width(b) > maxsize*scale:
            labels[b][labels[b] == i+1] = 0
    return array(labels != 0, 'B')


def compute_segmentation(binary, scale):
    """Given a binary image, compute a complete segmentation into
    lines, computing both columns and text lines."""
    binary = array(binary, 'B')

    # start by removing horizontal black lines, which only
    # interfere with the rest of the page segmentation
    binary = remove_hlines(binary, scale)

    # do the column finding
    if not args.quiet:
        print("computing column separators")
    colseps, binary = compute_colseps(binary, scale)

    # now compute the text line seeds
    if not args.quiet:
        print("computing lines")
    bottom, top, boxmap = compute_gradmaps(binary, scale)
    seeds = compute_line_seeds(binary, bottom, top, colseps, scale)
    ####imsave('/home/gupta/Documents/combinedseeds.png', [bottom,top,boxmap])
    # DSAVE("seeds",[bottom,top,boxmap])

    # spread the text line seeds to all the remaining
    # components
    if not args.quiet:
        print("propagating labels")
    llabels = morph.propagate_labels(boxmap, seeds, conflict=0)
    if not args.quiet:
        print("spreading labels")
    spread = morph.spread_labels(seeds, maxdist=scale)
    llabels = where(llabels > 0, llabels, spread*binary)
    segmentation = llabels*binary
    return segmentation


################################################################
# Processing each file.
################################################################

def process1(job):
    fname, i = job
    global base
    base, _ = ocrolib.allsplitext(fname)
    outputdir = base    

    try:
        binary = ocrolib.read_image_binary(base+".bin.png")
    except IOError:
        try:
            binary = ocrolib.read_image_binary(fname)
        except IOError:
            if ocrolib.trace:
                traceback.print_exc()
            print("cannot open either", base+".bin.png", "or", fname)
            return

    checktype(binary, ABINARY2)

    if not args.nocheck:
        check = check_page(amax(binary)-binary)
        if check is not None:
            print(fname, "SKIPPED", check, "(use -n to disable this check)")
            return

    if args.gray:
        if os.path.exists(base+".nrm.png"):
            gray = ocrolib.read_image_gray(base+".nrm.png")
        checktype(gray, GRAYSCALE)

    binary = 1-binary  # invert

    if args.scale == 0:
        scale = psegutils.estimate_scale(binary)
    else:
        scale = args.scale
    print("scale", scale)
    if isnan(scale) or scale > 1000.0:
        sys.stderr.write("%s: bad scale (%g); skipping\n" % (fname, scale))
        return
    if scale < args.minscale:
        sys.stderr.write("%s: scale (%g) less than --minscale; skipping\n" % (fname, scale))
        return

    # find columns and text lines

    if not args.quiet:
        print("computing segmentation")
    segmentation = compute_segmentation(binary, scale)
    if amax(segmentation) > args.maxlines:
        print(fname, ": too many lines", amax(segmentation))
        return
    if not args.quiet:
        print("number of lines", amax(segmentation))

    # compute the reading order

    if not args.quiet:
        print("finding reading order")
    lines = psegutils.compute_lines(segmentation, scale)
    order = psegutils.reading_order([l.bounds for l in lines])
    lsort = psegutils.topsort(order)

    # renumber the labels so that they conform to the specs

    nlabels = amax(segmentation)+1
    renumber = zeros(nlabels, 'i')
    for i, v in enumerate(lsort):
        renumber[lines[v].label] = 0x010000+(i+1)
    segmentation = renumber[segmentation]

    # finally, output everything

    if not args.quiet:
        print("writing lines")
    if not os.path.exists(outputdir):        
        os.mkdir(outputdir)
    lines = [lines[i] for i in lsort]
    ocrolib.write_page_segmentation("%s.pseg.png" % outputdir, segmentation)
    cleaned = ocrolib.remove_noise(binary, args.noise)
    for i, l in enumerate(lines):
        binline = psegutils.extract_masked(1-cleaned, l, pad=args.pad, expand=args.expand)
        ocrolib.write_image_binary("%s/01%04x.bin.png" % (outputdir, i+1), binline)
        if args.gray:
            grayline = psegutils.extract_masked(gray, l, pad=args.pad, expand=args.expand)
            ocrolib.write_image_gray("%s/01%04x.nrm.png" % (outputdir, i+1), grayline)
    print("%6d" % i, fname, "%4.1f" % scale, len(lines))


if len(args.files) == 1 and os.path.isdir(args.files[0]):
    files = glob.glob(args.files[0]+"/????.png")
else:
    files = args.files


def safe_process1(job):
    fname, i = job
    try:
        process1(job)
    except ocrolib.OcropusException as e:
        if e.trace:
            traceback.print_exc()
        else:
            print(fname, ":", e)
    except Exception as e:
        traceback.print_exc()


if args.parallel < 2:
    count = 0
    for i, f in enumerate(files):
        if args.parallel == 0:
            print(f)
        count += 1
        safe_process1((f, i+1))
else:
    pool = Pool(processes=args.parallel)
    jobs = []
    for i, f in enumerate(files):
        jobs += [(f, i+1)]
    result = pool.map(process1, jobs)
