import numpy as np
import scipy

import os, collections

from astroquery.gaia import Gaia

from astropy import units
from astropy.coordinates import SkyCoord
from astropy.convolution import Gaussian2DKernel
from astropy.io import ascii,fits
from astropy.nddata import Cutout2D,NDData
from astropy.stats import (sigma_clip, sigma_clipped_stats,
                           gaussian_fwhm_to_sigma,gaussian_sigma_to_fwhm)
from astropy.table import Table,Column,MaskedColumn,Row,vstack,setdiff,join
from astropy.wcs import WCS, utils as wcsutils

import photutils
from photutils.datasets import make_gaussian_sources_image
from photutils import Background2D, MedianBackground
from photutils import detect_sources, source_properties
from photutils.psf import EPSFModel, extract_stars
from photutils import EPSFBuilder, BoundingBox
from photutils import Background2D, MedianBackground
from photutils import EllipticalAperture, detect_threshold, deblend_sources
from photutils import CircularAperture , aperture_photometry , CircularAnnulus

import itertools
import copy
import pickle

from matplotlib import pyplot as plt, cm

#local
from .util import *

# astropy Table format for the gaia source catalog
_GAIACATFORMAT_ = 'ascii.ecsv'
_GAIACATEXT_ = 'ecsv'

# Column names for the magnitudes and S/N to use for selecting viable PSF stars
_GAIAMAGCOL_ =  'phot_rp_mean_mag'
_GAIAFLUXCOL_ =  'phot_rp_mean_flux'
_GAIAFLUXERRCOL_ =  'phot_rp_mean_flux_error'
_GAIASNCOL_ = 'phot_rp_mean_flux_over_error'

# astropy Table format for the fake SN source catalog
_FSNCATFORMAT_ = 'ascii.ecsv'
_FSNCATEXT_ = 'ecsv'

# Size of the box for each PSF star cutout (half width? or full?)
#  Does this also set the size of the resulting ePSF model?
_PSFSTARCUTOUTSIZE_ = 25 # pixels
_MAX_N_PLANTS_ = 999


class FitsImage:
    """A class to hold a single FITS image and associated products
    such as a PSF model, and detected source catalog.
    """
    def __init__(self, fitsfilename):
        """
        Constructor for FitsImage class. 

        Parameters
        ----------
        fitsfilename : str
            Name of fits file to read (can be .fits or .fits.fz)

        Returns
        -------
        FitsImage : :class:`~diffimageml.FitsImage`
        """

        self.filename = fitsfilename
        self.read_fits_file(fitsfilename)
        self.psfstars = None
        self.psfmodel = None
        self.epsf = None

        self.sourcecatalog = None
        self.zeropoint = None
        self.stellar_phot_table = None
        return

    def read_fits_file(self,fitsfilename):
        """
        Read in a fits file. 

        Parameters
        ----------
        fitsfilename : str
            Name of fits file to read (can be .fits or .fits.fz)

        Returns
        -------
        hdulist : :class:`~astropy.io.fits.HDUList`
        sci :  the science array :class:`~astropy.io.fits.PrimaryHDU` (or similar)

        """
        self.hdulist = fits.open(fitsfilename)
        self.filename = fitsfilename
        if 'SCI' in self.hdulist:
            self.sci = self.hdulist['SCI']
        else:
            for i in range(len(self.hdulist)):
                if self.hdulist[i].data is not None:
                    self.sci = self.hdulist[i]

        # image World Coord System
        self.wcs = WCS(self.sci.header)

        # Sky coordinate frame
        # TODO : Not sure we can expect the RADESYS keyword is always present.
        # Maybe there's an astropy function to get this in a more general way?
        self.frame = self.sci.header['RADESYS'].lower()

        return self.hdulist, self.sci


    def pixtosky(self,pixel):
        """
        Given a pixel location returns the skycoord
        """
        hdu = self.sci
        hdr = hdu.header
        wcs,frame = WCS(hdr),hdr['RADESYS'].lower()
        xp,yp = pixel
        sky = wcsutils.pixel_to_skycoord(xp,yp,wcs)
        return sky

    def skytopix(self,sky):
        """
        Given a skycoord (or list of skycoords) returns the pixel locations
        """
        hdu = self.sci
        hdr = hdu.header
        wcs,frame = WCS(hdr),hdr['RADESYS'].lower()
        pixel = wcsutils.skycoord_to_pixel(sky,wcs)
        return pixel

    @property
    def has_detections(self):
        """Check if a list of detected sources exists """
        return self.sourcecatalog is not None

    def detect_sources(self,nsigma=2,kfwhm=2.0,npixels=5,deblend=False,contrast=.001, **kwargs):
        """Detect sources (transient candidates) in the diff image using
        the astropy.photutils threshold-based source detection algorithm.

        Parameters
        ----------
        nsgima : float
            SNR required for pixel to be considered detected
        kfwhm : float
            FWHM of Circular Gaussian Kernel convolved on data to smooth noise
        npixels : int
            Number of connected pixels which are detected to give source
        deblend : bool
            Will use multiple levels/iterations to deblend single sources into multiple
        contrast : float
            If deblending the flux ratio required for local peak to be considered its own object

        Returns
        -------
        self.sourcecatalog: :class:`~photutils.segmentation.properties.SourceCatalog`
        """
        # TODO

        # record the locations and fluxes of candidate sources in an
        # external source catalog file (or a FITS extension)

        # if a fake is detected, mark it as such in the source catalog

        # if a fake is not detected, add it to the source catalog
        # (as a false negative)

        # maybe separate?: run aperture photometry on each fake source
        # maybe separate?: run PSF fitting photometry on each fake source
        # to be able to translate from ra/dec <--> pixels on image

        hdr = self.sci.header
        wcs,frame = WCS(hdr),hdr['RADESYS'].lower()
        #L1mean,L1med,L1sigma,L1fwhm = hdr['L1MEAN'],hdr['L1MEDIAN'],hdr['L1SIGMA'],hdr['L1FWHM'] # counts, fwhm in arcsec 
        #pixscale,saturate,maxlin = hdr['PIXSCALE'],hdr['SATURATE'],hdr['MAXLIN'] # arcsec/pixel, counts for saturation and non-linearity levels
        # if bkg None: detect threshold uses sigma clipped statistics to get bkg flux and set a threshold for detected sources
        # bkg also available in the hdr of file, either way is fine  
        # threshold = detect_threshold(hdu.data, nsigma=nsigma)
        # or you can provide a bkg of the same shape as data and this will be used
        boxsize=100
        bkg = Background2D(self.sci.data,boxsize) # sigma-clip stats for background est over image on boxsize, regions interpolated to give final map 
        threshold = detect_threshold(self.sci.data, nsigma=nsigma,background=bkg.background)
        ksigma = kfwhm * gaussian_fwhm_to_sigma  # FWHM pixels for kernel smoothing
        # optional ~ kernel smooths the image, using gaussian weighting
        kernel = Gaussian2DKernel(ksigma)
        kernel.normalize()
        # make a segmentation map, id sources defined as n connected pixels above threshold (n*sigma + bkg)
        segm = detect_sources(self.sci.data,
                              threshold, npixels=npixels, filter_kernel=kernel)
        # deblend useful for very crowded image with many overlapping objects...
        # uses multi-level threshold and watershed segmentation to sep local peaks as ind obj
        # use the same number of pixels and filter as was used on original segmentation
        # contrast is fraction of source flux local pk has to be consider its own obj
        if deblend:
            segm = deblend_sources(self.sci.data, 
                                           segm, npixels=5,filter_kernel=kernel, 
                                           nlevels=32,contrast=contrast)
        # need bkg subtracted to do photometry using source properties
        data_bkgsub = self.sci.data - bkg.background
        cat = source_properties(data_bkgsub, segm,background=bkg.background,
                                error=None,filter_kernel=kernel)

        # TODO the detection parameters into meta of table
        meta = {'detect_params':{"nsigma":nsigma,"kfwhm":kfwhm,"npixels":npixels,
                                                "deblend":deblend,"contrast":contrast}}

        self.sourcecatalog = cat 
        return self.sourcecatalog
    

    def detect_host_galaxies(self , ellipticity_cut = 0.35 , cut_cr = True ,**kwargs):
        """Detect sources  in the sky image using the astropy.photutils threshold-based
         source detection algorithm to get data on the host galaxies. Will attempt to identify
         the galaxies in the image
         '''

        Parameters
        ----------

        ellipticity_cut : float : We will select galaxies to be objects with an ellipticity
        greater than ellipticity_cut.
        
        cur_cr : boolean : If true, performs an additional cut on the number of pixels in the source
        in order to reduce the number of artifacts that get flagged as galaxies.
            
        Returns
        -------
        
        self.hostgalaxies : array : contains information on all host galaxies in the image
        """
        
        xcol = []
        ycol = []
        source_propertiescol = []
        
        if not self.has_detections:
            self.detect_sources(**kwargs)
        for i in self.sourcecatalog:
            if i.ellipticity > 0.35: ##Identifies Galaxies
                if i.area.value < 8 and cut_cr: ##Removes cosmic rays
                    continue
                xcol.append(i.centroid[1])
                ycol.append(i.centroid[0])
                source_propertiescol.append(i)
        hostgalaxies = Table([xcol , ycol , source_propertiescol] , names = ("x" , "y" , "Source Properties"))
                
        
        self.hostgalaxies = hostgalaxies
        return self.hostgalaxies

    
    def add_psf(self, psf, posflux, subshape=None,writetodisk=False,saveas="planted.fits"):
        """
        Add (or Subtract) PSF/PRFs from an image.

        Parameters
        ----------
        data : `~astropy.nddata.NDData` or array (must be 2D)
            Image data.
        psf : `astropy.modeling.Fittable2DModel` instance
            PSF/PRF model to be substracted from the data.
        posflux : Array-like of shape (3, N) or `~astropy.table.Table`
            Positions and fluxes for the objects to subtract.  If an array,
            it is interpreted as ``(x, y, flux)``  If a table, the columns
            'x_fit', 'y_fit', and 'flux_fit' must be present.
        subshape : length-2 or None
            The shape of the region around the center of the location to
            subtract the PSF from.  If None, subtract from the whole image.

        Returns
        -------
        subdata : same shape and type as ``data``
            The image with the PSF subtracted
        """

        # copying so can leave original data untouched
        hdu = self.sci
        cphdu = hdu.copy()
        data = cphdu.data
        cphdr = cphdu.header

        wcs,frame = WCS(cphdr),cphdr['RADESYS'].lower()

        if data.ndim != 2:
            raise ValueError(f'{data.ndim}-d array not supported. Only 2-d '
                             'arrays can be passed to subtract_psf.')

        #  translate array input into table
        if hasattr(posflux, 'colnames'):
            if 'x_fit' not in posflux.colnames:
                raise ValueError('Input table does not have x_fit')
            if 'y_fit' not in posflux.colnames:
                raise ValueError('Input table does not have y_fit')
            if 'flux_fit' not in posflux.colnames:
                raise ValueError('Input table does not have flux_fit')
        else:
            posflux = Table(names=['x_fit', 'y_fit', 'flux_fit'], data=posflux)

        # Set up contstants across the loop
        psf = psf.copy()
        xname, yname, fluxname = _extract_psf_fitting_names(psf)
        indices = np.indices(data.shape)
        subbeddata = data.copy()
        addeddata = data.copy()
        
        n = 0
        if subshape is None:
            indicies_reversed = indices[::-1]

            for row in posflux:
                getattr(psf, xname).value = row['x_fit']
                getattr(psf, yname).value = row['y_fit']
                getattr(psf, fluxname).value = row['flux_fit']

                xp,yp,flux_fit = row['x_fit'],row['y_fit'],row['flux_fit']
                sky = wcsutils.pixel_to_skycoord(xp,yp,wcs)
                idx = str(n).zfill(3) 
                cphdr['FK{}X'.format(idx)] = xp
                cphdr['FK{}Y'.format(idx)] = yp
                cphdr['FK{}RA'.format(idx)] = str(sky.ra.hms)
                cphdr['FK{}DEC'.format(idx)] = str(sky.dec.dms)
                cphdr['FK{}F'.format(idx)] = flux_fit
                # TO-DO, once have actual epsf classes will be clearer to fill the model
                cphdr['FK{}MOD'.format(idx)] = "NA"
                n += 1

                subbeddata -= psf(*indicies_reversed)
                addeddata += psf(*indicies_reversed)
        else:
            for row in posflux:
                x_0, y_0 = row['x_fit'], row['y_fit']

                # float dtype needed for fill_value=np.nan
                y = extract_array(indices[0].astype(float), subshape, (y_0, x_0))
                x = extract_array(indices[1].astype(float), subshape, (y_0, x_0))

                getattr(psf, xname).value = x_0
                getattr(psf, yname).value = y_0
                getattr(psf, fluxname).value = row['flux_fit']

                xp,yp,flux_fit = row['x_fit'],row['y_fit'],row['flux_fit']
                sky = wcsutils.pixel_to_skycoord(xp,yp,wcs)
                idx = str(n).zfill(3) 
                cphdr['FK{}X'.format(idx)] = xp
                cphdr['FK{}Y'.format(idx)] = yp
                cphdr['FK{}RA'.format(idx)] = str(sky.ra.hms)
                cphdr['FK{}DEC'.format(idx)] = str(sky.dec.dms)
                cphdr['FK{}F'.format(idx)] = flux_fit
                # TO-DO, once have actual epsf classes will be clearer to fill the model
                cphdr['FK{}MOD'.format(idx)] = "NA"
                n += 1
                
                subbeddata = add_array(subbeddata, -psf(x, y), (y_0, x_0))
                addeddata = add_array(addeddata, psf(x, y), (y_0, x_0))
        
        # the copied hdu written/returned should have data with the added psfs 
        cphdu.data = addeddata
        # inserting some new header values
        cphdr['fakeSN']=True 
        cphdr['N_fake']=str(len(posflux))
        cphdr['F_epsf']=str(psf.flux)
        
        if writetodisk:
            fits.writeto(saveas,cphdu.data,cphdr,overwrite=True)
        
        self.plants = [cphdu,posflux]
        self.has_fakes = True # if makes it through this plant_fakes update has_fakes

        return cphdu
    def fetch_gaia_sources(self, save_suffix='GaiaCat', overwrite=False,
                           verbose=False):
        """Using astroquery, download a list of sources from the Gaia
         catalog that are within the bounds of this image.

        Parameters
        ----------

        save_suffix: str
            If None, do not save to disk. If provided, save the Gaia source
            catalog to an ascii text file named as
            <rootname_of_this_fits_file>_<save_suffix>.<_GAIACATEXT_>

        overwrite: boolean
            When True, fetch from the remote Gaia database even if a local
            copy exists.  Write over the local file with the results from the
            remote db.

        self.gaia_catalog : Astropy Table : contains information on all
        Gaia sources in the image

        """
        #  when save_suffix is provided, check first to see if a
        #  catalog exists, and load the sources from there
        if save_suffix:
            root = os.path.splitext(os.path.splitext(self.filename)[0])[0]
            savefilename = root + '_' + save_suffix + '.' + _GAIACATEXT_
            if os.path.isfile(savefilename) and not overwrite:
                print("Gaia catalog {} exists. \n".format(savefilename) + \
                      "Reading without fetching.")
                self.read_gaia_sources(save_suffix=save_suffix)
                return

        # coord of central reference pixel
        ra_ref = self.sci.header['CRVAL1']
        dec_ref = self.sci.header['CRVAL2']
        coord = SkyCoord(ra_ref, dec_ref, unit=(units.deg, units.deg))

        ## Compute the pixel scale in units of arcseconds, from the CD matrix
        #cd11 = self.sci.header['CD1_1'] # deg/pixel
        #cd12 = self.sci.header['CD1_2'] # deg/pixel
        #cd21 = self.sci.header['CD2_1'] # deg/pixel
        #cd22 = self.sci.header['CD2_2'] # deg/pixel
        #cdmatrix = [[cd11,cd12],[cd21,cd22]]
        #pixelscale = np.sqrt(np.abs(np.linalg.det(cdmatrix))) * u.deg
        pixelscale = np.sqrt(wcsutils.proj_plane_pixel_area(self.wcs))

        # compute the width and height of the image from the NAXIS keywords
        naxis1 = self.sci.header['NAXIS1']
        naxis2 = self.sci.header['NAXIS2']
        width = naxis1 * pixelscale * units.deg
        height = naxis2 * pixelscale * units.deg

        # Do the search. Returns an astropy Table
        full_gaia_source_table = Gaia.query_object_async(
            coordinate=coord, width=width, height=height)

        # isolate the parameters of interest: ra,dec,r_mag
        racol = Column(data=full_gaia_source_table['ra'], name='ra')
        deccol = Column(data=full_gaia_source_table['dec'], name='dec')
        magcol = MaskedColumn(data=full_gaia_source_table[_GAIAMAGCOL_],
                              name='mag')

        # Compute a magnitude error (yes, its asymmetric. OK)
        flux=full_gaia_source_table[_GAIAFLUXCOL_]
        fluxerr=full_gaia_source_table[_GAIAFLUXERRCOL_]
        magerr = 1.086 * fluxerr/flux
        magerrcol = MaskedColumn(data=magerr, name='magerr')

        sncol = MaskedColumn(data=full_gaia_source_table[_GAIASNCOL_],
                              name='signal_to_noise')

        # add columns  x and y (pixel locations on image)
        #sky_positions= []
        pixel_positions=[]
        for i in range(len(full_gaia_source_table)):
            sky_pos = SkyCoord(ra=racol[i], dec=deccol[i],
                               unit=units.deg, frame=self.frame)
            #sky_positions.append(sky_pos)
            pixel_pos = wcsutils.skycoord_to_pixel(sky_pos, self.wcs)
            pixel_positions.append(pixel_pos)
        xcol = Column([pos[0] for pos in pixel_positions], name='x')
        ycol = Column([pos[1] for pos in pixel_positions], name='y')

        # create a minimalist Table
        self.gaia_source_table = Table(
            [racol, deccol, xcol, ycol, magcol, magerrcol, sncol])

        if verbose:
            print('There are {} stars available within fov '
                  'from gaia results queried'.format(
                len(self.gaia_source_table)))

        if save_suffix:
            if os.path.exists(savefilename):
                os.remove(savefilename)
            # TODO : make more space-efficient as a binary table?
            self.gaia_source_table.write(
                savefilename, format=_GAIACATFORMAT_)
            self.gaia_source_table.savefilename = savefilename

        return


    def read_gaia_sources(self, save_suffix='GaiaCat'):
        """Read in an existing catalog of sources from the Gaia
         database that are within the bounds of this image.

        Requires that fetch_gaia_sources() has previously been run,
        with save_suffix provided to save the catalog as an ascii
        text file named as
        <rootname_of_this_fits_file>_<save_suffix>.<_GAIACATEXT_>

        Parameters
        ----------

        save_suffix: str
            The suffix of the Gaia source catalog filename.
        """
        root = os.path.splitext(os.path.splitext(self.filename)[0])[0]
        catfilename = root + '_' + save_suffix + '.' + _GAIACATEXT_
        if not os.path.isfile(catfilename):
            print("Error: {} does not exist.".format(catfilename))
            return -1
        self.gaia_source_table = Table.read(
            catfilename, format=_GAIACATFORMAT_)
        return 0


    def plot_gaia_sources(self, magmin=12, magmax=18):
        """Show the locations of Gaia sources on the image.
        """
        # get the x,y pixel locations of all the sources in the image
        try:
            assert(self.gaia_source_table is not None)
        except assertionerror:
            print("No Gaia source table. Run fetch_gaia_sources()")
            return

        medpixval = np.median(self.sci.data)
        sigmapixval = np.std(self.sci.data)
        plt.imshow(self.sci.data, cmap=cm.Greys, interpolation='nearest',
                   aspect='equal', origin='lower', vmin=medpixval-sigmapixval,
                   vmax=medpixval+sigmapixval)

        mag = self.gaia_source_table['mag']
        imaglim = np.where( (magmin<mag) & (mag<magmax))[0]
        xsources = self.gaia_source_table['x'][imaglim]
        ysources = self.gaia_source_table['y'][imaglim]
        plt.plot(xsources, ysources, marker='o', ms=10, mfc='None',
                 mec='cyan', ls=' ', alpha=0.5)
        plt.title("Locations of Gaia Sources with {}<mag<{}".format(
            magmin,magmax))

        # TODO : plot diamonds over sources selected for PSF fitting

        # TODO : show cut-outs of stars used for PSF fitting
        return



    def do_stellar_photometry(self , gaia_catalog):
        """Takes in a source catalog for stars in the image from Gaia. Will perform
        aperture photometry on the sources listed in this catalog.

        Parameters
        ----------

        gaia_catalog: Astropy Table : Contains information on Gaia sources in the image
        
        self.stellar_phot_table : Astropy Table : Table containing the measured magnitudes
        for the stars in the image obtained from the Gaia catalog.
        
        """
        
        ##TODO: Add something to handle overstaturated sources
        ##TODO: Improve aperture sizes
        ##We currently just ignore anything brighter than m = 16 to avoid saturated sources
        
        positions = []
        
        for i in gaia_catalog:
        
            if i['mag'] < 16:
                continue
                
            positions.append( ( i['x'] , i['y'] ) ) ##Pixel coords for each source
        
        ##Set up the apertures
        apertures = CircularAperture(positions, r= 10)
        
        annulus_aperture = CircularAnnulus(positions, r_in = 15 , r_out = 20)
        annulus_masks = annulus_aperture.to_mask(method='center')
        
        ##Background subtraction using sigma clipped stats.
        ##Uses a median value from the annulus
        bkg_median = []
        for mask in annulus_masks:
            annulus_data = mask.multiply(self.sci.data)
            annulus_data_1d = annulus_data[mask.data > 0]
            _ , median_sigclip, _ = sigma_clipped_stats(annulus_data_1d)
            bkg_median.append(median_sigclip)
            
        ##Perform photometry and subtract out background
        bkg_median = np.array(bkg_median)
        phot = aperture_photometry(self.sci.data, apertures)
        phot['annulus_median'] = bkg_median
        phot['aper_bkg'] = bkg_median * apertures.area
        
        
        phot['aper_sum_bkgsub'] = phot['aperture_sum'] - phot['aper_bkg']
        
        phot['mag'] = -2.5 * np.log10( phot['aper_sum_bkgsub'] )
        
        self.stellar_phot_table = phot
        
        return


    def plot_stellar_photometry(self):
        """Simple plot of the stellar photometry results"""
        # TODO: update to show a comparison of aperture vs psf photometry

        try:
            assert(self.stellar_phot_table is not None)
        except:
            print("Missing measured stellar photometry. "
                  " Run do_stellar_photometry")
            return -1

        flux = self.stellar_phot_table['aper_sum_bkgsub']
        fluxerr = np.sqrt(self.stellar_phot_table['aper_bkg'])
        measured_mag = self.stellar_phot_table['mag']
        xphot = self.stellar_phot_table['xcenter']
        yphot = self.stellar_phot_table['ycenter']

        plt.errorbar(measured_mag+25, flux, fluxerr, ls=' ', marker='o')
        ax = plt.gca()
        ax.invert_xaxis()
        ax.set_yscale('log')
        plt.ylabel('Measured flux [counts]')
        plt.xlabel('Measured magnitude, assuming zpt=25')

        return


    def measure_zeropoint(self, showplot=False):
        """Measure the zeropoint of the image, using a set of
        known star locations and magnitudes, plus photutils aperture
        photometry of those stars.

        NOTE: currently using made-up data!!
        """
        try:
            assert(self.stellar_phot_table is not None)
        except:
            print("Missing measured stellar photometry. "
                  " Run do_stellar_photometry")
            return -1

        try:
            assert(self.gaia_source_table is not None)
        except:
            print("Missing Gaia catalog photometry. "
                  " Run fetch_gaia_sources")
            return -1

        star_flux = self.stellar_phot_table['aper_sum_bkgsub']
        star_flux_err = np.sqrt(self.stellar_phot_table['aper_bkg'])
        measured_mag = self.stellar_phot_table['mag']

        xphot = self.stellar_phot_table['xcenter']
        yphot = self.stellar_phot_table['ycenter']
        xcat = self.gaia_source_table['x']
        ycat = self.gaia_source_table['y']

        # Find the nearest Gaia catalog source for each measured star
        icat = []
        for i in range(len(xphot)):
            dist = np.sqrt((xphot[i].value - xcat)**2 +
                           (yphot[i].value - ycat)**2)
            icat.append(dist.argmin())
        star_mag = self.gaia_source_table['mag'][icat]
        star_mag_err = self.gaia_source_table['magerr']

        # mask non-positive flux measurements and those with S/N<20
        #star_flux_ma = np.ma.masked_less_equal(star_flux, 0, copy=True)
        ivalid = np.where( (star_flux>0) &
                           (np.abs(star_flux/star_flux_err)>20))
        nvalid = len(ivalid)

        # measure the zeropoint from each star
        zpt_fit = star_mag[ivalid] + 2.5 * np.log10(star_flux[ivalid])
        zpt_fit_err = np.sqrt(star_mag_err[ivalid]**2 +
                              (1.086 * star_flux_err[ivalid]
                               / star_flux[ivalid])**2 )

        # A dizzying array of ways to compute the zeropoint for the image
        zpt_mean_sc, zpt_median_sc, zpt_stdev_sc = sigma_clipped_stats(zpt_fit)
        #zpt_weighted_mean = np.average( zpt_fit, weights=1/zpt_fit_err**2)
        #zpt_fit_sigclipped = sigma_clip(zpt_fit, masked=True)
        #zpt_fit_err_sigclipped = zpt_fit_err[~zpt_fit_sigclipped.mask]
        #zpt_weighted_mean_sigclipped = np.average(
        #    zpt_fit_sigclipped[~zpt_fit_sigclipped.mask],
        #    weights=1/zpt_fit_err_sigclipped**2)

        self.zeropoint = zpt_median_sc

        if showplot:
            ax = plt.gca()
            plt.errorbar(star_mag[ivalid], zpt_fit, zpt_fit_err,
                         marker='.', ls=' ', color='k',
                         label='_nolabel_')
            skiptheselines = """
            ax.axhline(np.average(zpt_fit), color='darkorange',
                       label='{:.2f} naive mean, unclipped'.format(
                           np.average(zpt_fit)
                       ))

            ax.axhline(zpt_weighted_mean, color='red',
                       label='{:.2f} inv-var-wgtd mean, unclipped'.format(
                           zpt_weighted_mean
                       ))
            ax.axhline(zpt_mean_sc, color='teal',
                       label='{:.2f} sigma-clipped mean'.format(
                           zpt_mean_sc
                       ))
            ax.axhline(zpt_weighted_mean_sigclipped, color='blue',
                       label='{:.2f} sigma-clipped weighted mean'.format(
                           zpt_weighted_mean_sigclipped
                       ))
            """
            ax.axhline(zpt_median_sc, color='g',
                       label='{:.2f} sigma-clipped median'.format(
                           zpt_median_sc
                       ))
            plt.xlabel('Stellar Magnitude from Catalog')
            plt.ylabel('Inferred Zero Point')
            ax.legend(loc='best')

        return


    def extract_psf_stars(self, SNthresh=100, verbose=False):
        """
        Extract postage-stamp image cutouts of stars from the image, for use
        in building an ePSF model

        Parameters
        ----------

        SNthresh: float:  signal to noise threshold. Only stars with
        S/N > SNthresh are used for PSF construction.

        verbose: bool : verbose output
        """
        gaiacat = self.gaia_source_table
        image = self.sci


        # Define bounding boxes for the extractions so we can remove
        # any stars with overlaps. We want stars without overlaps
        # so the PSF construction doesn't require any deblending.
        # TODO : allow user to set the overlap size, or set based on FWHM
        bboxes = []
        for i in gaiacat:
            x = i['x']
            y = i['y']
            size = 25
            ixmin, ixmax = int(x - size/2), int(x + size/2)
            iymin, iymax = int(y - size/2), int(y + size/2)

            bbox = BoundingBox(ixmin=ixmin, ixmax=ixmax, iymin=iymin, iymax=iymax)
            bboxes.append(bbox)
        bboxes = Column(bboxes)
        gaiacat.add_column(bboxes,name='bbox')

        # using the bbox of each star from results to determine intersections,
        # dont want confusion of multi-stars for ePSF
        intersections = []
        for i,obj1 in enumerate(bboxes):
            for j in range(i+1,len(bboxes)):
                obj2 = bboxes[j]
                if obj1.intersection(obj2):
                    #print(obj1,obj2)
                    # these are the ones to remove
                    intersections.append(obj1)
                    intersections.append(obj2)
        # use the intersections found to remove stars
        j=0
        rows=[]
        for i in gaiacat:
            if i['bbox'] in intersections:
                #tmp.remove(i)
                row=j
                rows.append(row)
            j+=1
        gaiacat.remove_rows(rows)
        if verbose:
            print('{} stars, after removing intersections'.format(len(gaiacat)))

        # Limit to just stars with very good S/N
        gaiacat_trimmed = gaiacat[gaiacat['signal_to_noise']>SNthresh]
        if verbose:
            print('restricting extractions to stars w/ S/N > {}' 
                  'we have {} to consider'.format(
                SNthresh, len(gaiacat_trimmed)))

        # TODO? sort by the strongest signal/noise in r' filter
        # r.sort('phot_rp_mean_flux_over_error')
        """
        # don't think it will be necessary to limit to some N stars, might as well take all that will give good data for building psf
        if Nbrightest == None:
            Nbrightest = len(r)
        brightest_results = r[:Nbrightest]
        """

        data = image.data
        hdr = image.header
        # the header has L1 bkg values; should be the same as sigma clipped stats
        L1mean,L1med,L1sigma,L1fwhm = hdr['L1MEAN'],hdr['L1MEDIAN'],hdr['L1SIGMA'],hdr['L1FWHM'] # counts, fwhm in arcsec
        mean_val, median_val, std_val = sigma_clipped_stats(data, sigma=2.)
        WMSSKYBR = hdr['WMSSKYBR'] # mag/arcsec^2 of sky bkg measured
        # AGGMAG the guide star magnitude header value would be simpler but it is given as unknown, ra/dec are provided for it though
        # grab some other useful header values now
        pixscale,saturate,maxlin = hdr['PIXSCALE'],hdr['SATURATE'],hdr['MAXLIN'] # arcsec/pixel, counts for saturation and non-linearity levels

        # need bkg subtracted to extract stars, want to build ePSF using just star brightness
        data -= median_val # L1med
        nddata = NDData(data=data)
        psfstars_extracted = extract_stars(nddata, catalogs=gaiacat,
                                           size=_PSFSTARCUTOUTSIZE_)
        # using the bbox of each star from results to determine intersections,
        # we don't want confusion of blended stars in our ePSF
        intersections = []
        for i,obj1 in enumerate(psfstars_extracted.bbox):
            for j in range(i+1,len(psfstars_extracted.bbox)):
                obj2 = psfstars_extracted.bbox[j]
                if obj1.intersection(obj2):
                    #print(obj1,obj2)
                    # these are the ones to remove
                    intersections.append(obj1)
                    intersections.append(obj2)
        # use the intersections found to remove stars
        tmp = [i for i in psfstars_extracted] # get a list of stars rather than single photutils obj with all of them
        for i in tmp:
            if i.bbox in intersections:
                tmp.remove(i)
        if verbose:
            print('{} stars, after removing intersections'.format(len(tmp)))

        # note ref.fits doesn't have saturate and maxlin available
        # the image should be just one of the trims
        for i in tmp:
            if np.max(i.data) > saturate:
                tmp.remove(i)
            elif np.max(i.data) > maxlin:
                tmp.remove(i)

        if verbose:
            print('removed stars above saturation or non-linearity level'
                  '~ {}, {} ADU; now have {}'.format(
                saturate,maxlin,len(tmp)))
        psf_stars_selected = photutils.psf.EPSFStars(tmp)

        """
        # you should look at the images to make sure these are good stars
        nrows = 4
        ncols = 4
        fig, ax = plt.subplots(nrows=nrows, ncols=ncols, figsize=(20, 20),
                                squeeze=True)
        ax = ax.ravel()
        for i in range(len(brightest_results)):
            norm = simple_norm(stars[i], 'log', percent=99.)
            ax[i].imshow(stars[i], norm=norm, origin='lower', cmap='viridis')
        """
        self.psfstars = psf_stars_selected

        return

    def build_epsf_model(self, oversampling=2,
                         verbose=False, save_suffix=None, overwrite=False):
        """Build an effective PSF model from a set of stars in the image
        Uses a list of star locations (from Gaia)  which are below
        non-linearity/saturation

        Parameters
        ----------

        oversampling: int : the oversampling scale for the PSF model. See the
          photutils ePSF model documentation for details.

        verbose: bool : verbose output

        save_suffix: str
            The suffix for the epsf model output filename.
            If set to None, then no output file is generated

        overwrite: bool
            If True, overwrite any existing ePSF model saved as a .pkl file
            If False, and a .pkl exists with the name indicated by save_suffix,
            just read that in without remaking the PSF model.

        """
        # check for existence of pre-made PSF model and load it if desired
        rootfilename = os.path.splitext(
            os.path.splitext(self.filename)[0])[0]
        if save_suffix is not None and overwrite == False:
            epsf_filename = rootfilename + '_' + save_suffix + '.pkl'
            if os.path.isfile(epsf_filename):
                self.load_epsfmodel_from_pickle(save_suffix=save_suffix)
                return


        # check for existence of gaia source table and fetch/read it if needed
        catfilename = rootfilename + '_' + save_suffix + '.' + _GAIACATEXT_
        if os.path.isfile(catfilename):
            try:
                self.read_gaia_sources(save_suffix=save_suffix)
            except:
                print("Tried to read existing Gaia source table... failed.")
        if self.psfstars is None:
            self.extract_psf_stars(verbose=verbose)
        assert(self.psfstars is not None)

        # TODO: accommodate other header keywords to get the stats we need
        hdr = self.sci.header
        L1mean = hdr['L1MEAN'] # for LCO: counts
        L1med  = hdr['L1MEDIAN'] # for LCO: counts
        L1sigma = hdr['L1SIGMA'] # for LCO: counts
        L1fwhm = hdr['L1FWHM'] # for LCO: fwhm in arcsec
        pixscale = hdr['PIXSCALE'] # arcsec/pixel
        saturate = hdr['SATURATE'] # counts (saturation level)
        maxlin = hdr['MAXLIN'] # counts (max level for linear pixel response)

        # oversampling chops pixels of each star up further to get better fit
        # this is okay since stacking multiple ...
        # however more oversampled the ePSF is, the more stars you need to get
        # smooth result
        # LCO is already oversampling the PSFs, the fwhm ~ 2 arcsec while
        # pixscale ~ 0.4 arcsec; should be able to get good ePSF measurement
        # without any oversampling
        # ePSF basic x,y,sigma 3 param model should be easily obtained if
        # consider that 3*pixscale < fwhm
        epsf_builder = EPSFBuilder(oversampling=oversampling, maxiters=10,
                                   progress_bar=True)
        epsf, fitted_stars = epsf_builder(self.psfstars)

        self.epsf = epsf
        self.fitted_stars = fitted_stars

        if self.zeropoint is None:
            self.measure_zeropoint()

        # TODO : Can we save this ePSF model as a fits extension instead?
        if save_suffix:
            # Write out the ePSF model
            # TODO: make a function for generating output file names
            rootfilename = os.path.splitext(
                os.path.splitext(self.filename)[0])[0]
            epsf_filename = rootfilename + '_' + save_suffix + '.pkl'
            pickle.dump( self.epsf, open( epsf_filename, "wb" ) )

        return

    def load_epsfmodel_from_pickle(self, save_suffix):
        """Read in an ePSF model from a pickle file

        Parameters
        ----------

        save_suffix: str
            The suffix for the epsf model filename to be read.
        """
        rootfilename = os.path.splitext(
            os.path.splitext(self.filename)[0])[0]
        epsf_filename = rootfilename + '_' + save_suffix + '.pkl'
        self.epsf = pickle.load(open( epsf_filename, "rb" ) )
        return


    def plot_epsf_model(self):
        try :
            assert(self.epsf is not None)
        except:
            print("No ePSF model exists. Run build_epsf_model()")
            return -1
        plt.imshow(self.epsf.data, interpolation='Nearest', origin='lower')
        plt.colorbar()


    def write_to_catalog(self , save_suffix = "fakecat" , overwrite = False , add_to = False, add_to_filename = None):
        
    
        """
        
        Writes information for fake sources into a fake source catalog
        Will include the locations and epsf models for all of the fake sources
        
        

        Parameters
        ----------

        save_suffix: str
            If None, do not save to disk. If provided, save the fake source
            catalog to an ascii text file named as
            <rootname_of_this_fits_file>_<save_suffix>.<_GAIACATEXT_>

        overwrite: boolean
            When True, overwrite an existing fake sn catalog
            Otherwise, will only save catalog if it does not already exist
            
            
        add_to_filename: str
            If None, this is ignored. If provided, the souce catalog from this
            image will be appended to the given file. Designed to create a catalog
            containing fake sources from multiple images. Will still produce a catalog
            for this image using save_suffix, unless save_suffix = None
        
        self.fakesncat : Astropy Table : Contains information on the fake sources and
        their host galaxies
        
        """
        
        
        fakes = []
        file_header = self.hdulist[0].header
        
        # If we are not adding to an existing file
        if save_suffix != None:
            root = os.path.splitext(os.path.splitext(self.filename)[0])[0]
            savename = root + "_" + save_suffix + "." + _FSNCATEXT_
            
            ##If file exists and not overwrite, exit now.
            if os.path.exists(savename) and not overwrite:
                print ("Warning: Fake SN catalog exists. Will not overwrite, so we won't save the catalog.")
                savename = None
                
        elif save_suffix == None: ##Don't save catalog for this image
            savename = None
            
        
        RA = []
        DEC = []
        SCA = []
        F = []
        MOD = []
        X = []
        Y = []
        for i in file_header.keys():
             if i[0:2] == "FK" and int(i[2:5]) not in fakes: #Identify header entries for fake SN
                N = i[2:5]
                fakes.append(int(N))
                RA.append(file_header["FK" + str(N) + "RA"])
                DEC.append(file_header["FK" + str(N) + "DEC"])
                SCA.append(file_header["FK" + str(N) + "SCA"])
                F.append(file_header["FK" + str(N) + "MOD"])
                MOD.append(file_header["FK" + str(N) + "MOD"])
                X.append(file_header["FK" + str(N) + "X"])
                Y.append(file_header["FK" + str(N) + "Y"])

        racol = Column(RA , name = "ra")
        deccol = Column(DEC , name = "dec")
        scacol = Column(SCA , name = "sca")
        fcol = Column(F , name = "F")
        modcol = Column(MOD , name = "mod")
        xcol = Column(X , name = "x")
        ycol = Column(Y , name = "y")
        
        
        if savename != None: ##Writes (or overwrites) new file 
        
            self.fakesncat = Table([racol , deccol , scacol , fcol , modcol , xcol , ycol])
            self.fakesncat.write( savename , format =_FSNCATFORMAT_ , overwrite = True)
            
        elif add_to_filename != None:
        
            if os.path.exists(add_to_filename): 
                ##File exists, so we add to the existing catalog
                
                self.read_fakesn_catalog(filename = add_to_filename)
                new_table = Table([racol , deccol , scacol , fcol , modcol , xcol , ycol])
                combined_table = vstack([self.fakesncat , new_table])
                combined_table.write(add_to_filename , format = _FSNCATFORMAT_ , overwrite = True)
                
            else:
                ##File does not exist, so we make one
                
                self.fakesncat = Table([racol , deccol , scacol , fcol , modcol , xcol , ycol])
                self.fakesncat.write( add_to_filename , format =_FSNCATFORMAT_ , overwrite = True)
             
        return
        
    def read_fakesn_catalog(self , save_suffix = "fakecat" , filename = None):
        """
        
        Reads in a fake source catalog
        

        Parameters
        ----------

        save_suffix: str
            If provided, read the fake sourc catalog named as
            <rootname_of_this_fits_file>_<save_suffix>.<_GAIACATEXT_>
            Will be ignored if a filename is provided

        filename: str
            If provided, will read in a catalog with this filename. Overwrites
            any save_suffix that is provided
        
        self.fakesncat : Astropy Table : Contains information on the fake sources and
        their host galaxies
        
        """
        
        if filename != None:
            root = os.path.splitext(os.path.splitext(self.filename)[0])[0]
            readname = root + "_" + save_suffix + "." + _FSNCATEXT_
        else:
            readname = filename
        
        self.fakesncat = Table.read(readname , format =_FSNCATFORMAT_)
            
            
class FakePlanter:
    """A class for handling the FITS file triplets (diff,search,ref),
    planting fakes, detecting fakes, and creating sub-images and
    catalogs for use in training+validation of machine learning
    algorithms
    """

    def __init__(self, diffim_fitsfilename,
                 searchim_fitsfilename=None,
                 templateim_fitsfilename=None):
        """Read in a triplet of three FITS files that hold
        A. a difference image
        B. a 'search' image (typically a "new" single-epoch static sky image)
        C. the template image (or 'reference')

        The diff image was constructed as the subtraction of the search minus
        the template:   A = B - C
        Generally this is not a straight subtraction operation, as we apply
        PSF convolution or other data modification with something like the
        Alard & Lupton or ZOGY algorithm.

        """
        # Read in the three fits files that hold the diff images
        self.diffim = FitsImage(diffim_fitsfilename)
        if searchim_fitsfilename:
            self.searchim = FitsImage(searchim_fitsfilename)
        if templateim_fitsfilename:
            self.templateim = FitsImage(templateim_fitsfilename)

        # has_fakes False until run plant_fakes
        self.has_fakes = False
        # has_lco_epsf False until run lco_epsf
        self.has_lco_epsf = False
        # detection_efficiency None until calculated
        self.detection_efficiency = None
        return


    @property
    def has_epsfmodel(self):
        """True if both the diffim and searchim have an ePSF model.
        Otherwise False.
        """
        if ( self.diffim.psfmodel is not None and
            self.searchim.psfmodel is not None ):
            return ( type(self.diffim.psfmodel) == EPSFModel and
                     type(self.searchim.psfmodel) == EPSFModel)
        return False


    def get_lensed_locations(self,phis,ds,fluxes=None):
        """
        Simulate lensed SN positions on host-galaxy ellipse

        Parameters
        ----------
        phis : List or array (must be 1D)
            Supernova angles CCW from host semimajor axis. degs
        ds : List or array (must be 1D)
            Supernova distances from host center. pixels
        fluxes: Default None will generate list same length as positions of constant flux = 10**4
            List or Array (must be 1D same length as positions) will set the fluxes 
        Returns
        -------
        posflux : Array-like of shape (3, N) or `~astropy.table.Table`
                Positions and fluxes for the objects to subtract.  If an array,
                it is interpreted as ``(x, y, flux)``  If a table, the columns
                'x_fit', 'y_fit', and 'flux_fit' must be present.
        """
        
        # the host galaxy properties will be taken from the template which should have best detection
        hostgalaxies = self.templateim.hostgalaxies
        hostgalaxy = hostgalaxies[0].to_table()
        
        # the pixel location from the centroid of detection on template
        x = hostgalaxy["xcentroid"][0].value # pix
        y = hostgalaxy["ycentroid"][0].value # pix
        location = (x,y)
        
        # the search/diff locations will use their corresponding pixel locations for this sky location
        # needs to be included in the case that search or diff isn't sized the same as template
        template_wcs = self.templateim.wcs
        sky = wcsutils.pixel_to_skycoord(x,y,template_wcs)
        search_wcs = self.searchim.wcs
        diff_wcs = self.diffim.wcs
        search_location = wcsutils.skycoord_to_pixel(sky,search_wcs) 
        x_search,y_search = search_location
        diff_location = wcsutils.skycoord_to_pixel(sky,diff_wcs)
        x_diff, y_diff = diff_location

        #xtheta ytheta defined analytically for segm image using variance then partial theta ~ 0 gives an ellipse 
        a = hostgalaxy["semimajor_axis_sigma"][0].value # pix
        b = hostgalaxy["semiminor_axis_sigma"][0].value # pix
        orientation = hostgalaxy["orientation"][0].value # deg a-axis ccw from +x
        
        xs,ys,locs = [],[],[]
        xs_search,ys_search,locs_search = [],[],[]
        xs_diff,ys_diff,locs_diff = [],[],[]

        for i in range(len(phis)):
            phi = phis[i]
            d = ds[i]
            xi = x+d*np.cos((orientation+phi)*np.pi/180)
            yi = y+d*np.sin((orientation+phi)*np.pi/180)
            xs.append(xi)
            ys.append(yi)
            locs.append((xi,yi))
            # now the search 
            xi = x_search+d*np.cos((orientation+phi)*np.pi/180)
            yi = y_search+d*np.sin((orientation+phi)*np.pi/180)
            xs_search.append(xi)
            ys_search.append(yi)
            locs_search.append((xi,yi))
            # now the diff
            xi = x_diff+d*np.cos((orientation+phi)*np.pi/180)
            yi = y_diff+d*np.sin((orientation+phi)*np.pi/180)
            xs_diff.append(xi)
            ys_diff.append(yi)
            locs_diff.append((xi,yi))

        
        # if don't provide list of fluxes they will be set as constant flux        
        if not fluxes:
            flux = 10**4
            fluxes = [flux for i in range(len(locs))]

        # put into table ready for entry as photutils subtract_psf posflux arg
        posflux = Table(data=[xs,ys,fluxes],names=["x_fit","y_fit","flux_fit"],)
        posflux_search = Table(data=[xs_search,ys_search,fluxes],names=["x_fit","y_fit","flux_fit"])
        posflux_diff = Table(data=[xs_diff,ys_diff,fluxes],names=["x_fit","y_fit","flux_fit"])
        
        self.templateim.lensed_locations = posflux
        self.searchim.lensed_locations = posflux_search
        self.diffim.lensed_locations = posflux_diff

        return [posflux_diff,posflux_search,posflux]

    def added_triplet(self,epsf,posfluxes):
        """
        Function for add_psf using epsf and posfluxes to the triplet of three FITS files that hold
        A. a difference image
        B. a 'search' image (typically a "new" single-epoch static sky image)
        C. the template image (or 'reference')

        The triplet remains unchanged, access to the planted for each available as self.im.plants
        Returns list [self.diffim.plants,self.searchim.plants,self.templateim.plants] 
        """

        # TODO could also use self.im.lensed_locations
        # posfluxes needs to be generalized more carefully
        # posfluxes assuming list of posflux returned as diff,search,template 
        diffplants = self.diffim.add_psf(epsf,posfluxes[0])
        searchplants = self.searchim.add_psf(epsf,posfluxes[1])
        templateplants = self.templateim.add_psf(epsf,posfluxes[2])

        return [diffplants,searchplants,templateplants]


    def postage_stamp_triplet(self,location,size):
        """
        Function for making postage stamps at given location and size for the triplet of three FITS files that hold
        A. a difference image
        B. a 'search' image (typically a "new" single-epoch static sky image)
        C. the template image (or 'reference')

        The triplet remains unchanged, access to the postage stamp for each pristine data available as self.im.postage_stamp
        or for the added_psf triplet as self.im.plants.postage_stamp
        Returns list [[self.diffim.plants,self.searchim.plants,self.templateim.plants],
                    [diffim.plants.postage_stamp,searchim.plants.postage_stamp,templatim.plants.postage_stamp]] 
        """
        diffim = self.diffim
        searchim = self.searchim
        templateim = self.templateim

        # [0] is the hdu with added data and updated header,
        # [1] would be the posfluxes (available in the hdrs)
        diffplant = diffim.plants[0]
        searchplant = searchim.plants[0]
        templateplant = templateim.plants[0]

        diff_ps = cut_hdu(diffim,location,size)
        search_ps = cut_hdu(searchim,location,size)
        template_ps = cut_hdu(templateim,location,size)
        clean_ps = [diff_ps,search_ps,template_ps]

        diffplant_ps = cut_hdu(diffplant,location,size)
        searchplant_ps = cut_hdu(searchplant,location,size)
        templateplant_ps = cut_hdu(templateplant,location,size)
        added_ps = [diffplant_ps,searchplant_ps,templateplant_ps]

        return [clean_ps,added_ps]

    def has_fakes(self):
        """Check if fake stars have been planted in the image"""
        return self.has_fakes

    def plant_fakes(self,epsf,locations,SCA=None,writetodisk=False,saveas="planted.fits"):
        """Function for planting fake stars in the diff image.
        """
        # using the ePSF model embedded in the fits file, plant a grid
        # of fakes or plant fakes around galaxies with varying magnitudes
        # (fluxes), mimicking strong-lensing sources

        # write info into the fits header for each planted fake, including
        # the location and total flux

        # optional:  write out the modified image with fakes planted as
        # a new fits file record in the image db that fakes have been
        # planted in the image

        hdu = self.diffim.sci # the fits opened difference image hdu

        # copying so can leave original data untouched
        cphdu = hdu.copy()
        cpim = cphdu.data
        cphdr = cphdu.header
        
        wcs,frame = WCS(cphdr),cphdr['RADESYS'].lower()
        
        # location should be list of pixels [(x1,y1),(x2,y2)...(xn,yn)]
        n = 0
        for pix in locations:
            pix = list(pix)
            xp,yp = pix
            sky = wcsutils.pixel_to_skycoord(xp,yp,wcs)
            idx = str(n).zfill(3) 
            cphdr['FK{}X'.format(idx)] = xp
            cphdr['FK{}Y'.format(idx)] = yp
            cphdr['FK{}RA'.format(idx)] = str(sky.ra.hms)
            cphdr['FK{}DEC'.format(idx)] = str(sky.dec.dms)

            if SCA:
                # SCA ~ scaling factor for epsf, epsf*sca, needs to be list of floats same length as locations 
                sca = SCA[n]
                epsfn = epsf*sca
            else:
                # SCA ~ None, all the same brightness of input epsf
                sca = 1
                epsfn = epsf*sca
            cphdr['FK{}SCA'.format(idx)] = sca
            cphdr['FK{}F'.format(idx)] = np.sum(epsfn)

            # TO-DO, once have actual epsf classes will be clearer to fill the model
            cphdr['FK{}MOD'.format(idx)] = "NA"

            revpix = copy.copy(pix)
            revpix.reverse()
            row,col=revpix
            nrows,ncols=epsf.shape
            # +2 in these to grab a couple more than needed, the correct shapes for broadcasting taken using actual psf.shapes
            rows=np.arange(int(np.round(row-nrows/2)),int(np.round(row+nrows/2))+2) 
            cols=np.arange(int(np.round(col-ncols/2)),int(np.round(col+ncols/2))+2) 
            rows = rows[:epsf.shape[0]]
            cols = cols[:epsf.shape[1]]
            cpim[rows[:, None], cols] += epsfn
            np.float64(cpim)

            n+=1
        
        # inserting some new header values
        cphdr['fakeSN']=True 
        cphdr['N_fake']=str(len(locations))
        cphdr['F_epsf']=str(np.sum(epsf))
        
        if writetodisk:
            fits.writeto(saveas,cpim,cphdr,overwrite=True)
        
        self.has_fakes = True # if makes it through this plant_fakes update has_fakes

        return cphdu

    def confusion_matrix(self,fp_detections=None):
        """Function for creating confusion matrix of detections vs plants
        """

        #TO-DO decide what the confusion_matrix shoud look like
        #imagining using the plant fits header for the plant detections and the threshold parameters for fp detections

        #plant_detections a yet to be defined property
        #will be something like a catalog/file with rows for each planted object 
        #plants have a col for detect ~ 1 is detection (TP), 0 is non-detection (FN)
        plants = self.plant_detections
        
        #fp_detections is the same type of catalog/file from plants detection but run using the clean diff
        #default None will run it here
        #need to make sure detection flag gets updated in plant hdr during the efficiency function once thats in here
        
        TP = [] # detected plant
        FN = [] # not detected plant
        FP = [] # detected, but not a plant, (all the detections on clean diffim)
        TN = None # not detected not a plant, (no meaning)
        for i in plants:
            if i['detect'] == 1:
                TP.append(i)
            elif i['detect'] == 0:
                FN.append(i)
        TP = vstack(TP)
        FN = vstack(FN)
        
        if fp_detections:
            FP = fp_detections
        else:
            # TO-DO set the parameters in detect_sources using vals from run on the plant 
            # self.detection_vals = [nsigma,kfwhm,npixels,deblend,contrast]
            FP = detect_sources(self.diffim.sci)
        
        return [TP,FN,FP,TN]
    
    def get_fake_locations(self,image_with_fakes):
        fake_plant_x_keys = [key for key in image_with_fakes.header.keys() if\
                         'FK' in key and 'X' in key]
        fake_plant_x = [image_with_fakes.header[key] for key in fake_plant_x_keys]
        fake_plant_y = []
        fakeIDs = []
        for key in fake_plant_x_keys:
            fake_id = key[2:2+len(str(_MAX_N_PLANTS_))]
            fakeIDs.append(fake_id)
            fake_plant_y.append(image_with_fakes.header['FK%sY'%fake_id])
        fake_positions = np.array([fake_plant_x,fake_plant_y]).T
        return fakeIDs,fake_positions

    def set_fake_detection_header(self,image_with_fakes,detection_table=None,outfilename=None):
        if detection_table is None:
            detection_table = self.detection_table

        for row in detection_table:
            image_with_fakes.header['FK%sDET'%row['fakeID']] = row['detected']
        if isinstance(outfilename,str):
            fits.writeto(outfilename,image_with_fakes,image_with_fakes.header,overwrite=True)
        return image_with_fakes
        

    @property
    def has_detection_efficiency(self):
        return self.detection_efficiency is not None

    def calculate_detection_efficiency(self,image_with_fakes=None,
                fake_plant_locations=None,source_catalog=None,gridSize = 2,**kwargs):
        """
        Given a difference image with fake sources planted and a detected 
        source catalog, will calculate the detection efficiency.

        Parameters
        ----------
        image_with_fakes : `~fakeplanting.FitsImage`
            A fits image class containing the planted fake sources (default self.diffim)
        fake_plant_locations : list or `~numpy.ndarray`
            2D array containing the x,y locations of the fake sources (default read self.diffim.sci.header)
        source_catalog : :class:`~photutils.segmentation.properties.SourceCatalog`
            Detected source catalog 

        Returns
        -------
        detection_efficiency : float
        detection_table : `~astropy.table.Table` with ID,xy-locations,detected (1 or 0)
        """
        if image_with_fakes is None:
            image_with_fakes = self.diffim

        if source_catalog is None:
            if isinstance(image_with_fakes,FitsImage):
                if image_with_fakes.has_detections:
                    source_catalog = image_with_fakes.sourcecatalog
                else:
                    source_catalog = self.diffim.detect_sources(**kwargs)
            else:
                raise RuntimeError("If image_with_fakes is not of type FitsImage, must provide a source_catalog.")

        if fake_plant_locations is None:
            if isinstance(image_with_fakes,FitsImage):
                fake_plant_ids,fake_plant_locations = self.get_fake_locations(image_with_fakes.sci)
            else:
                fake_plant_ids,fake_plant_locations = self.get_fake_locations(image_with_fakes)
        # use locations and a search radius on detections and plant locations to get true positives
        tbl = source_catalog.to_table()
        tbl_x,tbl_y = [i.value for i in tbl['xcentroid']], [i.value for i in tbl['ycentroid']]
        tbl_pixels = list(zip(tbl_x,tbl_y))        
        search = gridSize # fwhm*n might be better criteria

        truths = []
        binary_detection_dict = {key:0 for key in fake_plant_ids}
        for pixel in tbl_pixels:
            for ind in range(len(fake_plant_locations)):
                i = fake_plant_locations[ind]
                if pixel[0] > i[0] - search  and pixel[0] < i[0] + search and pixel[1] > i[1] - search and pixel[1] < i[1] + search:
                    truths.append([tuple(i),pixel])
                    binary_detection_dict[fake_plant_ids[ind]] = 1
                    break # TODO Think about multiple detections
                else:
                    continue

        plant_pixels = []
        det_src_pixels = []
        for i in truths:
            plant_pix = i[0]
            det_src_pix = i[1]
            if plant_pix not in plant_pixels:
                plant_pixels.append(plant_pix)
                det_src_pixels.append(det_src_pix)
        
        N_plants_detected = len(plant_pixels)
        efficiency = N_plants_detected/len(fake_plant_locations)
        binary_detection = [binary_detection_dict[fkID] for fkID in fake_plant_ids]
        detection_table = Table([fake_plant_ids,fake_plant_locations[:,0],fake_plant_locations[:,1],binary_detection],
                    names=['fakeID','pixX','pixY','detected'])
        
        if isinstance(image_with_fakes,FitsImage):
            image_with_fakes = self.set_fake_detection_header(image_with_fakes = image_with_fakes.sci,detection_table = detection_table)
        else:
            image_with_fakes = self.set_fake_detection_header(image_with_fakes = image_with_fakes,detection_table = detection_table)
        self.detection_efficiency = efficiency
        self.detection_table = detection_table
        self.diffim = image_with_fakes
        return self.detection_efficiency,self.detection_table
