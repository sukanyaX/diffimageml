"""
===================
Basic Functionality
===================

Basic usage of the diffimageml package.
"""
		
###############################################################
# Explain here what this example does
   
import diffimageml

_SEARCHIM1_ = '../test_data/sky_image_1.fits.fz'
assert(os.path.isfile(_SEARCHIM1_))
searchim = diffimageml.FitsImage(_SEARCHIM1_)

# ## Fetch a catalog of stars in the image from the Gaia db
# and show fetched gaia stars
searchim.fetch_gaia_sources(overwrite=False)
searchim.plot_gaia_sources(magmin=12, magmax=18)


# ## Do Photometry of the Gaia Stars
searchim.do_stellar_photometry(searchim.gaia_source_table)

# show photometry of the gaia stars
searchim.plot_stellar_photometry()


# ## Measure the zero point for this image from the Gaia stars
searchim.measure_zeropoint(showplot=True)


# ## Build an ePSF Model from the Gaia stars that are not saturated
# TODO : show the ePSF model building

