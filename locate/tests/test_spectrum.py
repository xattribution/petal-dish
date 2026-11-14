import numpy as np

from petal_locate.core.spectrum import welch_psd_db


def test_unit_noise_reads_zero_db():
    rng = np.random.default_rng(1)
    n = 1024 * 64
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)
    psd = welch_psd_db(x, 1e5, 1024, 64)
    assert psd.dtype == np.float32 and psd.shape == (1024,)
    assert abs(float(np.mean(10 ** (psd / 10))) - 1.0) < 0.03


def test_tone_lands_in_expected_bin():
    fs, nfft = 1e5, 1024
    k = 100
    t = np.arange(nfft * 4) / fs
    x = np.exp(2j * np.pi * (k * fs / nfft) * t)
    psd = welch_psd_db(x, fs, nfft, 4)
    assert int(np.argmax(psd)) == nfft // 2 + k
