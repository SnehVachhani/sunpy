# -*- coding: utf-8 -*-

import astropy.units as u
from astropy.tests.helper import assert_quantity_allclose

from ..utilities import *


def test_get_earth():
    # Validate against published values from the Astronomical Almanac (2013)
    e1 = get_earth('2013-Jan-01')
    assert e1.lon == 0*u.deg
    assert_quantity_allclose(e1.lat, -3.03*u.deg, atol=5e-3*u.deg)
    assert_quantity_allclose(e1.radius, 0.9832947*u.AU, atol=5e-7*u.AU)

    e2 = get_earth('2013-Sep-01')
    assert e2.lon == 0*u.deg
    assert_quantity_allclose(e2.lat, 7.19*u.deg, atol=5e-3*u.deg)
    assert_quantity_allclose(e2.radius, 1.0092561*u.AU, atol=5e-7*u.AU)


def test_get_sun_B0():
    # Validate against published values from the Astronomical Almanac (2013)
    assert_quantity_allclose(get_sun_B0('2013-Apr-01'), -6.54*u.deg, atol=5e-3*u.deg)
    assert_quantity_allclose(get_sun_B0('2013-Dec-01'), 0.88*u.deg, atol=5e-3*u.deg)

    # Validate against a published value from Astronomical Algorithms (Meeus 1998, p.191)
    assert_quantity_allclose(get_sun_B0('1992-Oct-13'), 5.99*u.deg, atol=5e-3*u.deg)


def test_get_sun_L0():
    # Validate against published values from the Astronomical Almanac (2013)
    assert_quantity_allclose(get_sun_L0('2013-Apr-01'), 221.44*u.deg, atol=2e-2*u.deg)
    assert_quantity_allclose(get_sun_L0('2013-Dec-01'), 237.83*u.deg, atol=2e-2*u.deg)

    # Validate against a published value from Astronomical Algorithms (Meeus 1998, p.191)
    assert_quantity_allclose(get_sun_L0('1992-Oct-13'), 238.6317*u.deg, atol=5e-5*u.deg)


def test_get_sun_P():
    # Validate against published values from the Astronomical Almanac (2013)
    assert_quantity_allclose(get_sun_P('2013-Apr-01'), -26.15*u.deg, atol=1e-2*u.deg)
    assert_quantity_allclose(get_sun_P('2013-Dec-01'), 16.05*u.deg, atol=1e-2*u.deg)
