from __future__ import absolute_import, division, print_function, unicode_literals

import math

import torch

__all__ = [
    "istft",
    "spectrogram",
    "griffinlim",
    "amplitude_to_DB",
    "create_fb_matrix",
    "create_dct",
    "mu_law_encoding",
    "mu_law_decoding",
    "complex_norm",
    "angle",
    "magphase",
    "phase_vocoder",
    "lfilter",
    "lowpass_biquad",
    "highpass_biquad",
    "equalizer_biquad",
    "biquad",
    'mask_along_axis',
    'mask_along_axis_iid'
]


# TODO: remove this once https://github.com/pytorch/pytorch/issues/21478 gets solved
@torch.jit.ignore
def _stft(
    waveform,
    n_fft,
    hop_length,
    win_length,
    window,
    center,
    pad_mode,
    normalized,
    onesided,
):
    # type: (Tensor, int, Optional[int], Optional[int], Optional[Tensor], bool, str, bool, bool) -> Tensor
    return torch.stft(
        waveform,
        n_fft,
        hop_length,
        win_length,
        window,
        center,
        pad_mode,
        normalized,
        onesided,
    )


def istft(
    stft_matrix,  # type: Tensor
    n_fft,  # type: int
    hop_length=None,  # type: Optional[int]
    win_length=None,  # type: Optional[int]
    window=None,  # type: Optional[Tensor]
    center=True,  # type: bool
    pad_mode="reflect",  # type: str
    normalized=False,  # type: bool
    onesided=True,  # type: bool
    length=None,  # type: Optional[int]
):
    # type: (...) -> Tensor
    r"""Inverse short time Fourier Transform. This is expected to be the inverse of torch.stft.
    It has the same parameters (+ additional optional parameter of ``length``) and it should return the
    least squares estimation of the original signal. The algorithm will check using the NOLA condition (
    nonzero overlap).

    Important consideration in the parameters ``window`` and ``center`` so that the envelop
    created by the summation of all the windows is never zero at certain point in time. Specifically,
    :math:`\sum_{t=-\infty}^{\infty} w^2[n-t\times hop\_length] \cancel{=} 0`.

    Since stft discards elements at the end of the signal if they do not fit in a frame, the
    istft may return a shorter signal than the original signal (can occur if ``center`` is False
    since the signal isn't padded).

    If ``center`` is True, then there will be padding e.g. 'constant', 'reflect', etc. Left padding
    can be trimmed off exactly because they can be calculated but right padding cannot be calculated
    without additional information.

    Example: Suppose the last window is:
    [17, 18, 0, 0, 0] vs [18, 0, 0, 0, 0]

    The n_frame, hop_length, win_length are all the same which prevents the calculation of right padding.
    These additional values could be zeros or a reflection of the signal so providing ``length``
    could be useful. If ``length`` is ``None`` then padding will be aggressively removed
    (some loss of signal).

    [1] D. W. Griffin and J. S. Lim, "Signal estimation from modified short-time Fourier transform,"
    IEEE Trans. ASSP, vol.32, no.2, pp.236-243, Apr. 1984.

    Args:
        stft_matrix (torch.Tensor): Output of stft where each row of a channel is a frequency and each
            column is a window. it has a size of either (..., fft_size, n_frame, 2)
        n_fft (int): Size of Fourier transform
        hop_length (Optional[int]): The distance between neighboring sliding window frames.
            (Default: ``win_length // 4``)
        win_length (Optional[int]): The size of window frame and STFT filter. (Default: ``n_fft``)
        window (Optional[torch.Tensor]): The optional window function.
            (Default: ``torch.ones(win_length)``)
        center (bool): Whether ``input`` was padded on both sides so
            that the :math:`t`-th frame is centered at time :math:`t \times \text{hop\_length}`.
            (Default: ``True``)
        pad_mode (str): Controls the padding method used when ``center`` is True. (Default:
            ``'reflect'``)
        normalized (bool): Whether the STFT was normalized. (Default: ``False``)
        onesided (bool): Whether the STFT is onesided. (Default: ``True``)
        length (Optional[int]): The amount to trim the signal by (i.e. the
            original signal length). (Default: whole signal)

    Returns:
        torch.Tensor: Least squares estimation of the original signal of size (..., signal_length)
    """
    stft_matrix_dim = stft_matrix.dim()
    assert 3 <= stft_matrix_dim, "Incorrect stft dimension: %d" % (stft_matrix_dim)
    assert stft_matrix.numel() > 0

    if stft_matrix_dim == 3:
        # add a channel dimension
        stft_matrix = stft_matrix.unsqueeze(0)

    # pack batch
    shape = stft_matrix.size()
    stft_matrix = stft_matrix.reshape(-1, shape[-3], shape[-2], shape[-1])

    dtype = stft_matrix.dtype
    device = stft_matrix.device
    fft_size = stft_matrix.size(1)
    assert (onesided and n_fft // 2 + 1 == fft_size) or (
        not onesided and n_fft == fft_size
    ), (
        "one_sided implies that n_fft // 2 + 1 == fft_size and not one_sided implies n_fft == fft_size. "
        + "Given values were onesided: %s, n_fft: %d, fft_size: %d"
        % ("True" if onesided else False, n_fft, fft_size)
    )

    # use stft defaults for Optionals
    if win_length is None:
        win_length = n_fft

    if hop_length is None:
        hop_length = int(win_length // 4)

    # There must be overlap
    assert 0 < hop_length <= win_length
    assert 0 < win_length <= n_fft

    if window is None:
        window = torch.ones(win_length, device=device, dtype=dtype)

    assert window.dim() == 1 and window.size(0) == win_length

    if win_length != n_fft:
        # center window with pad left and right zeros
        left = (n_fft - win_length) // 2
        window = torch.nn.functional.pad(window, (left, n_fft - win_length - left))
        assert window.size(0) == n_fft
    # win_length and n_fft are synonymous from here on

    stft_matrix = stft_matrix.transpose(1, 2)  # size (channel, n_frame, fft_size, 2)
    stft_matrix = torch.irfft(
        stft_matrix, 1, normalized, onesided, signal_sizes=(n_fft,)
    )  # size (channel, n_frame, n_fft)

    assert stft_matrix.size(2) == n_fft
    n_frame = stft_matrix.size(1)

    ytmp = stft_matrix * window.view(1, 1, n_fft)  # size (channel, n_frame, n_fft)
    # each column of a channel is a frame which needs to be overlap added at the right place
    ytmp = ytmp.transpose(1, 2)  # size (channel, n_fft, n_frame)

    eye = torch.eye(n_fft, device=device, dtype=dtype).unsqueeze(1)  # size (n_fft, 1, n_fft)

    # this does overlap add where the frames of ytmp are added such that the i'th frame of
    # ytmp is added starting at i*hop_length in the output
    y = torch.nn.functional.conv_transpose1d(
        ytmp, eye, stride=hop_length, padding=0
    )  # size (channel, 1, expected_signal_len)

    # do the same for the window function
    window_sq = (
        window.pow(2).view(n_fft, 1).repeat((1, n_frame)).unsqueeze(0)
    )  # size (1, n_fft, n_frame)
    window_envelop = torch.nn.functional.conv_transpose1d(
        window_sq, eye, stride=hop_length, padding=0
    )  # size (1, 1, expected_signal_len)

    expected_signal_len = n_fft + hop_length * (n_frame - 1)
    assert y.size(2) == expected_signal_len
    assert window_envelop.size(2) == expected_signal_len

    half_n_fft = n_fft // 2
    # we need to trim the front padding away if center
    start = half_n_fft if center else 0
    end = -half_n_fft if length is None else start + length

    y = y[:, :, start:end]
    window_envelop = window_envelop[:, :, start:end]

    # check NOLA non-zero overlap condition
    window_envelop_lowest = window_envelop.abs().min()
    assert window_envelop_lowest > 1e-11, "window overlap add min: %f" % (
        window_envelop_lowest
    )

    y = (y / window_envelop).squeeze(1)  # size (channel, expected_signal_len)

    # unpack batch
    y = y.reshape(shape[:-3] + y.shape[-1:])

    if stft_matrix_dim == 3:  # remove the channel dimension
        y = y.squeeze(0)

    return y


def spectrogram(
    waveform, pad, window, n_fft, hop_length, win_length, power, normalized
):
    # type: (Tensor, int, Tensor, int, int, int, Optional[int], bool) -> Tensor
    r"""Create a spectrogram or a batch of spectrograms from a raw audio signal.
    The spectrogram can be either magnitude-only or complex.

    Args:
        waveform (torch.Tensor): Tensor of audio of dimension (..., channel, time)
        pad (int): Two sided padding of signal
        window (torch.Tensor): Window tensor that is applied/multiplied to each frame/window
        n_fft (int): Size of FFT
        hop_length (int): Length of hop between STFT windows
        win_length (int): Window size
        power (int): Exponent for the magnitude spectrogram,
            (must be > 0) e.g., 1 for energy, 2 for power, etc.
            If None, then the complex spectrum is returned instead.
        normalized (bool): Whether to normalize by magnitude after stft

    Returns:
        torch.Tensor: Dimension (..., channel, freq, time), where channel
        is unchanged, freq is ``n_fft // 2 + 1`` and ``n_fft`` is the number of
        Fourier bins, and time is the number of window hops (n_frame).
    """

    if pad > 0:
        # TODO add "with torch.no_grad():" back when JIT supports it
        waveform = torch.nn.functional.pad(waveform, (pad, pad), "constant")

    # pack batch
    shape = waveform.size()
    waveform = waveform.reshape(-1, shape[-1])

    # default values are consistent with librosa.core.spectrum._spectrogram
    spec_f = _stft(
        waveform, n_fft, hop_length, win_length, window, True, "reflect", False, True
    )

    # unpack batch
    spec_f = spec_f.reshape(shape[:-1] + spec_f.shape[-3:])

    if normalized:
        spec_f /= window.pow(2).sum().sqrt()
    if power is not None:
        spec_f = spec_f.pow(power).sum(-1)  # get power of "complex" tensor

    return spec_f


def griffinlim(
    spectrogram, window, n_fft, hop_length, win_length, power, normalized, n_iter, momentum, length, rand_init
):
    # type: (Tensor, Tensor, int, int, int, int, bool, int, float, Optional[int], bool) -> Tensor
    r"""Compute waveform from a linear scale magnitude spectrogram using the Griffin-Lim transformation.
        Implementation ported from `librosa`.

    .. [1] McFee, Brian, Colin Raffel, Dawen Liang, Daniel PW Ellis, Matt McVicar, Eric Battenberg, and Oriol Nieto.
        "librosa: Audio and music signal analysis in python."
        In Proceedings of the 14th python in science conference, pp. 18-25. 2015.

    .. [2] Perraudin, N., Balazs, P., & Søndergaard, P. L.
        "A fast Griffin-Lim algorithm,"
        IEEE Workshop on Applications of Signal Processing to Audio and Acoustics (pp. 1-4),
        Oct. 2013.

    .. [3] D. W. Griffin and J. S. Lim,
        "Signal estimation from modified short-time Fourier transform,"
        IEEE Trans. ASSP, vol.32, no.2, pp.236–243, Apr. 1984.

    Args:
        spectrogram (torch.Tensor): A magnitude-only STFT spectrogram of dimension (channel, freq, frames)
            where freq is ``n_fft // 2 + 1``.
        window (torch.Tensor): Window tensor that is applied/multiplied to each frame/window
        n_fft (int): Size of FFT, creates ``n_fft // 2 + 1`` bins
        hop_length (int): Length of hop between STFT windows. (
            Default: ``win_length // 2``)
        win_length (int): Window size. (Default: ``n_fft``)
        power (int): Exponent for the magnitude spectrogram,
            (must be > 0) e.g., 1 for energy, 2 for power, etc. (Default: ``2``)
        normalized (bool): Whether to normalize by magnitude after stft. (Default: ``False``)
        n_iter (int): Number of iteration for phase recovery process.
        momentum (float): The momentum parameter for fast Griffin-Lim.
            Setting this to 0 recovers the original Griffin-Lim method.
            Values near 1 can lead to faster convergence, but above 1 may not converge. (Default: 0.99)
        length (Optional[int]): Array length of the expected output. (Default: ``None``)
        rand_init (bool): Initializes phase randomly if True, to zero otherwise. (Default: ``True``)

    Returns:
        torch.Tensor: waveform of (channel, time), where time equals the ``length`` parameter if given.
    """
    assert momentum < 1, 'momentum=%s > 1 can be unstable' % momentum
    assert momentum > 0, 'momentum=%s < 0' % momentum

    spectrogram = spectrogram.pow(1 / power)

    # randomly initialize the phase
    batch, freq, frames = spectrogram.size()
    if rand_init:
        angles = 2 * math.pi * torch.rand(batch, freq, frames)
    else:
        angles = torch.zeros(batch, freq, frames)
    angles = torch.stack([angles.cos(), angles.sin()], dim=-1) \
                  .to(dtype=spectrogram.dtype, device=spectrogram.device)
    spectrogram = spectrogram.unsqueeze(-1).expand_as(angles)

    # And initialize the previous iterate to 0
    rebuilt = torch.tensor(0.)

    for _ in range(n_iter):
        # Store the previous iterate
        tprev = rebuilt

        # Invert with our current estimate of the phases
        inverse = istft(spectrogram * angles,
                        n_fft=n_fft,
                        hop_length=hop_length,
                        win_length=win_length,
                        window=window,
                        length=length).float()

        # Rebuild the spectrogram
        rebuilt = _stft(inverse, n_fft, hop_length, win_length, window,
                        True, 'reflect', False, True)

        # Update our phase estimates
        angles = rebuilt - tprev.mul_(momentum / (1 + momentum))
        angles = angles.div_(complex_norm(angles).add_(1e-16).unsqueeze(-1).expand_as(angles))

    # Return the final phase estimates
    return istft(spectrogram * angles,
                 n_fft=n_fft,
                 hop_length=hop_length,
                 win_length=win_length,
                 window=window,
                 length=length)


def amplitude_to_DB(x, multiplier, amin, db_multiplier, top_db=None):
    # type: (Tensor, float, float, float, Optional[float]) -> Tensor
    r"""Turn a tensor from the power/amplitude scale to the decibel scale.

    This output depends on the maximum value in the input tensor, and so
    may return different values for an audio clip split into snippets vs. a
    a full clip.

    Args:
        x (torch.Tensor): Input tensor before being converted to decibel scale
        multiplier (float): Use 10. for power and 20. for amplitude
        amin (float): Number to clamp ``x``
        db_multiplier (float): Log10(max(reference value and amin))
        top_db (Optional[float]): Minimum negative cut-off in decibels. A reasonable number
            is 80. (Default: ``None``)

    Returns:
        torch.Tensor: Output tensor in decibel scale
    """
    x_db = multiplier * torch.log10(torch.clamp(x, min=amin))
    x_db -= multiplier * db_multiplier

    if top_db is not None:
        x_db = x_db.clamp(min=x_db.max().item() - top_db)

    return x_db


def create_fb_matrix(n_freqs, f_min, f_max, n_mels, sample_rate):
    # type: (int, float, float, int, int) -> Tensor
    r"""Create a frequency bin conversion matrix.

    Args:
        n_freqs (int): Number of frequencies to highlight/apply
        f_min (float): Minimum frequency (Hz)
        f_max (float): Maximum frequency (Hz)
        n_mels (int): Number of mel filterbanks
        sample_rate (int): Sample rate of the audio waveform

    Returns:
        torch.Tensor: Triangular filter banks (fb matrix) of size (``n_freqs``, ``n_mels``)
        meaning number of frequencies to highlight/apply to x the number of filterbanks.
        Each column is a filterbank so that assuming there is a matrix A of
        size (..., ``n_freqs``), the applied result would be
        ``A * create_fb_matrix(A.size(-1), ...)``.
    """
    # freq bins
    # Equivalent filterbank construction by Librosa
    all_freqs = torch.linspace(0, sample_rate // 2, n_freqs)
    i_freqs = all_freqs.ge(f_min) & all_freqs.le(f_max)
    freqs = all_freqs[i_freqs]

    # calculate mel freq bins
    # hertz to mel(f) is 2595. * math.log10(1. + (f / 700.))
    m_min = 2595.0 * math.log10(1.0 + (f_min / 700.0))
    m_max = 2595.0 * math.log10(1.0 + (f_max / 700.0))
    m_pts = torch.linspace(m_min, m_max, n_mels + 2)
    # mel to hertz(mel) is 700. * (10**(mel / 2595.) - 1.)
    f_pts = 700.0 * (10 ** (m_pts / 2595.0) - 1.0)
    # calculate the difference between each mel point and each stft freq point in hertz
    f_diff = f_pts[1:] - f_pts[:-1]  # (n_mels + 1)
    slopes = f_pts.unsqueeze(0) - all_freqs.unsqueeze(1)  # (n_freqs, n_mels + 2)
    # create overlapping triangles
    zero = torch.zeros(1)
    down_slopes = (-1.0 * slopes[:, :-2]) / f_diff[:-1]  # (n_freqs, n_mels)
    up_slopes = slopes[:, 2:] / f_diff[1:]  # (n_freqs, n_mels)
    fb = torch.max(zero, torch.min(down_slopes, up_slopes))
    return fb


def create_dct(n_mfcc, n_mels, norm):
    # type: (int, int, Optional[str]) -> Tensor
    r"""Create a DCT transformation matrix with shape (``n_mels``, ``n_mfcc``),
    normalized depending on norm.

    Args:
        n_mfcc (int): Number of mfc coefficients to retain
        n_mels (int): Number of mel filterbanks
        norm (Optional[str]): Norm to use (either 'ortho' or None)

    Returns:
        torch.Tensor: The transformation matrix, to be right-multiplied to
        row-wise data of size (``n_mels``, ``n_mfcc``).
    """
    # http://en.wikipedia.org/wiki/Discrete_cosine_transform#DCT-II
    n = torch.arange(float(n_mels))
    k = torch.arange(float(n_mfcc)).unsqueeze(1)
    dct = torch.cos(math.pi / float(n_mels) * (n + 0.5) * k)  # size (n_mfcc, n_mels)
    if norm is None:
        dct *= 2.0
    else:
        assert norm == "ortho"
        dct[0] *= 1.0 / math.sqrt(2.0)
        dct *= math.sqrt(2.0 / float(n_mels))
    return dct.t()


def mu_law_encoding(x, quantization_channels):
    # type: (Tensor, int) -> Tensor
    r"""Encode signal based on mu-law companding.  For more info see the
    `Wikipedia Entry <https://en.wikipedia.org/wiki/%CE%9C-law_algorithm>`_

    This algorithm assumes the signal has been scaled to between -1 and 1 and
    returns a signal encoded with values from 0 to quantization_channels - 1.

    Args:
        x (torch.Tensor): Input tensor
        quantization_channels (int): Number of channels

    Returns:
        torch.Tensor: Input after mu-law encoding
    """
    mu = quantization_channels - 1.0
    if not x.is_floating_point():
        x = x.to(torch.float)
    mu = torch.tensor(mu, dtype=x.dtype)
    x_mu = torch.sign(x) * torch.log1p(mu * torch.abs(x)) / torch.log1p(mu)
    x_mu = ((x_mu + 1) / 2 * mu + 0.5).to(torch.int64)
    return x_mu


def mu_law_decoding(x_mu, quantization_channels):
    # type: (Tensor, int) -> Tensor
    r"""Decode mu-law encoded signal.  For more info see the
    `Wikipedia Entry <https://en.wikipedia.org/wiki/%CE%9C-law_algorithm>`_

    This expects an input with values between 0 and quantization_channels - 1
    and returns a signal scaled between -1 and 1.

    Args:
        x_mu (torch.Tensor): Input tensor
        quantization_channels (int): Number of channels

    Returns:
        torch.Tensor: Input after mu-law decoding
    """
    mu = quantization_channels - 1.0
    if not x_mu.is_floating_point():
        x_mu = x_mu.to(torch.float)
    mu = torch.tensor(mu, dtype=x_mu.dtype)
    x = ((x_mu) / mu) * 2 - 1.0
    x = torch.sign(x) * (torch.exp(torch.abs(x) * torch.log1p(mu)) - 1.0) / mu
    return x


def complex_norm(complex_tensor, power=1.0):
    # type: (Tensor, float) -> Tensor
    r"""Compute the norm of complex tensor input.

    Args:
        complex_tensor (torch.Tensor): Tensor shape of `(..., complex=2)`
        power (float): Power of the norm. (Default: `1.0`).

    Returns:
        torch.Tensor: Power of the normed input tensor. Shape of `(..., )`
    """
    if power == 1.0:
        return torch.norm(complex_tensor, 2, -1)
    return torch.norm(complex_tensor, 2, -1).pow(power)


def angle(complex_tensor):
    # type: (Tensor) -> Tensor
    r"""Compute the angle of complex tensor input.

    Args:
        complex_tensor (torch.Tensor): Tensor shape of `(..., complex=2)`

    Return:
        torch.Tensor: Angle of a complex tensor. Shape of `(..., )`
    """
    return torch.atan2(complex_tensor[..., 1], complex_tensor[..., 0])


def magphase(complex_tensor, power=1.0):
    # type: (Tensor, float) -> Tuple[Tensor, Tensor]
    r"""Separate a complex-valued spectrogram with shape `(..., 2)` into its magnitude and phase.

    Args:
        complex_tensor (torch.Tensor): Tensor shape of `(..., complex=2)`
        power (float): Power of the norm. (Default: `1.0`)

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: The magnitude and phase of the complex tensor
    """
    mag = complex_norm(complex_tensor, power)
    phase = angle(complex_tensor)
    return mag, phase


def phase_vocoder(complex_specgrams, rate, phase_advance):
    # type: (Tensor, float, Tensor) -> Tensor
    r"""Given a STFT tensor, speed up in time without modifying pitch by a
    factor of ``rate``.

    Args:
        complex_specgrams (torch.Tensor): Dimension of `(..., freq, time, complex=2)`
        rate (float): Speed-up factor
        phase_advance (torch.Tensor): Expected phase advance in each bin. Dimension
            of (freq, 1)

    Returns:
        complex_specgrams_stretch (torch.Tensor): Dimension of `(...,
        freq, ceil(time/rate), complex=2)`

    Example
        >>> freq, hop_length = 1025, 512
        >>> # (channel, freq, time, complex=2)
        >>> complex_specgrams = torch.randn(2, freq, 300, 2)
        >>> rate = 1.3 # Speed up by 30%
        >>> phase_advance = torch.linspace(
        >>>    0, math.pi * hop_length, freq)[..., None]
        >>> x = phase_vocoder(complex_specgrams, rate, phase_advance)
        >>> x.shape # with 231 == ceil(300 / 1.3)
        torch.Size([2, 1025, 231, 2])
    """

    # pack batch
    shape = complex_specgrams.size()
    complex_specgrams = complex_specgrams.reshape([-1] + list(shape[-3:]))

    time_steps = torch.arange(0,
                              complex_specgrams.size(-2),
                              rate,
                              device=complex_specgrams.device,
                              dtype=complex_specgrams.dtype)

    alphas = time_steps % 1.0
    phase_0 = angle(complex_specgrams[..., :1, :])

    # Time Padding
    complex_specgrams = torch.nn.functional.pad(complex_specgrams, [0, 0, 0, 2])

    # (new_bins, freq, 2)
    complex_specgrams_0 = complex_specgrams.index_select(-2, time_steps.long())
    complex_specgrams_1 = complex_specgrams.index_select(-2, (time_steps + 1).long())

    angle_0 = angle(complex_specgrams_0)
    angle_1 = angle(complex_specgrams_1)

    norm_0 = torch.norm(complex_specgrams_0, p=2, dim=-1)
    norm_1 = torch.norm(complex_specgrams_1, p=2, dim=-1)

    phase = angle_1 - angle_0 - phase_advance
    phase = phase - 2 * math.pi * torch.round(phase / (2 * math.pi))

    # Compute Phase Accum
    phase = phase + phase_advance
    phase = torch.cat([phase_0, phase[..., :-1]], dim=-1)
    phase_acc = torch.cumsum(phase, -1)

    mag = alphas * norm_1 + (1 - alphas) * norm_0

    real_stretch = mag * torch.cos(phase_acc)
    imag_stretch = mag * torch.sin(phase_acc)

    complex_specgrams_stretch = torch.stack([real_stretch, imag_stretch], dim=-1)

    # unpack batch
    complex_specgrams_stretch = complex_specgrams_stretch.reshape(shape[:-3] + complex_specgrams_stretch.shape[1:])

    return complex_specgrams_stretch


def lfilter(waveform, a_coeffs, b_coeffs):
    # type: (Tensor, Tensor, Tensor) -> Tensor
    r"""Perform an IIR filter by evaluating difference equation.

    Args:
        waveform (torch.Tensor): audio waveform of dimension of `(..., time)`.  Must be normalized to -1 to 1.
        a_coeffs (torch.Tensor): denominator coefficients of difference equation of dimension of `(n_order + 1)`.
                                Lower delays coefficients are first, e.g. `[a0, a1, a2, ...]`.
                                Must be same size as b_coeffs (pad with 0's as necessary).
        b_coeffs (torch.Tensor): numerator coefficients of difference equation of dimension of `(n_order + 1)`.
                                 Lower delays coefficients are first, e.g. `[b0, b1, b2, ...]`.
                                 Must be same size as a_coeffs (pad with 0's as necessary).

    Returns:
        output_waveform (torch.Tensor): Dimension of `(..., time)`.  Output will be clipped to -1 to 1.

    """

    dim = waveform.dim()

    # pack batch
    shape = waveform.size()
    waveform = waveform.reshape(-1, shape[-1])

    assert(a_coeffs.size(0) == b_coeffs.size(0))
    assert(len(waveform.size()) == 2)
    assert(waveform.device == a_coeffs.device)
    assert(b_coeffs.device == a_coeffs.device)

    device = waveform.device
    dtype = waveform.dtype
    n_channel, n_sample = waveform.size()
    n_order = a_coeffs.size(0)
    n_sample_padded = n_sample + n_order - 1
    assert(n_order > 0)

    # Pad the input and create output
    padded_waveform = torch.zeros(n_channel, n_sample_padded, dtype=dtype, device=device)
    padded_waveform[:, (n_order - 1):] = waveform
    padded_output_waveform = torch.zeros(n_channel, n_sample_padded, dtype=dtype, device=device)

    # Set up the coefficients matrix
    # Flip coefficients' order
    a_coeffs_flipped = a_coeffs.flip(0)
    b_coeffs_flipped = b_coeffs.flip(0)

    # calculate windowed_input_signal in parallel
    # create indices of original with shape (n_channel, n_order, n_sample)
    window_idxs = torch.arange(n_sample, device=device).unsqueeze(0) + torch.arange(n_order, device=device).unsqueeze(1)
    window_idxs = window_idxs.repeat(n_channel, 1, 1)
    window_idxs += (torch.arange(n_channel, device=device).unsqueeze(-1).unsqueeze(-1) * n_sample_padded)
    window_idxs = window_idxs.long()
    # (n_order, ) matmul (n_channel, n_order, n_sample) -> (n_channel, n_sample)
    input_signal_windows = torch.matmul(b_coeffs_flipped, torch.take(padded_waveform, window_idxs))

    for i_sample, o0 in enumerate(input_signal_windows.t()):
        windowed_output_signal = padded_output_waveform[:, i_sample:(i_sample + n_order)]
        o0.sub_(torch.mv(windowed_output_signal, a_coeffs_flipped))
        o0.div_(a_coeffs[0])

        padded_output_waveform[:, i_sample + n_order - 1] = o0

    output = torch.clamp(padded_output_waveform[:, (n_order - 1):], min=-1., max=1.)

    # unpack batch
    output = output.reshape(shape[:-1] + output.shape[-1:])

    return output


def biquad(waveform, b0, b1, b2, a0, a1, a2):
    # type: (Tensor, float, float, float, float, float, float) -> Tensor
    r"""Perform a biquad filter of input tensor.  Initial conditions set to 0.
    https://en.wikipedia.org/wiki/Digital_biquad_filter

    Args:
        waveform (torch.Tensor): audio waveform of dimension of `(channel, time)`
        b0 (float): numerator coefficient of current input, x[n]
        b1 (float): numerator coefficient of input one time step ago x[n-1]
        b2 (float): numerator coefficient of input two time steps ago x[n-2]
        a0 (float): denominator coefficient of current output y[n], typically 1
        a1 (float): denominator coefficient of current output y[n-1]
        a2 (float): denominator coefficient of current output y[n-2]

    Returns:
        output_waveform (torch.Tensor): Dimension of `(channel, time)`
    """

    device = waveform.device
    dtype = waveform.dtype

    output_waveform = lfilter(
        waveform,
        torch.tensor([a0, a1, a2], dtype=dtype, device=device),
        torch.tensor([b0, b1, b2], dtype=dtype, device=device)
    )
    return output_waveform


def _dB2Linear(x):
    # type: (float) -> float
    return math.exp(x * math.log(10) / 20.0)


def highpass_biquad(waveform, sample_rate, cutoff_freq, Q=0.707):
    # type: (Tensor, int, float, float) -> Tensor
    r"""Design biquad highpass filter and perform filtering.  Similar to SoX implementation.

    Args:
        waveform (torch.Tensor): audio waveform of dimension of `(channel, time)`
        sample_rate (int): sampling rate of the waveform, e.g. 44100 (Hz)
        cutoff_freq (float): filter cutoff frequency
        Q (float): https://en.wikipedia.org/wiki/Q_factor

    Returns:
        output_waveform (torch.Tensor): Dimension of `(channel, time)`
    """

    GAIN = 1.
    w0 = 2 * math.pi * cutoff_freq / sample_rate
    A = math.exp(GAIN / 40.0 * math.log(10))
    alpha = math.sin(w0) / 2. / Q
    mult = _dB2Linear(max(GAIN, 0))

    b0 = (1 + math.cos(w0)) / 2
    b1 = -1 - math.cos(w0)
    b2 = b0
    a0 = 1 + alpha
    a1 = -2 * math.cos(w0)
    a2 = 1 - alpha
    return biquad(waveform, b0, b1, b2, a0, a1, a2)


def lowpass_biquad(waveform, sample_rate, cutoff_freq, Q=0.707):
    # type: (Tensor, int, float, float) -> Tensor
    r"""Design biquad lowpass filter and perform filtering.  Similar to SoX implementation.

    Args:
        waveform (torch.Tensor): audio waveform of dimension of `(channel, time)`
        sample_rate (int): sampling rate of the waveform, e.g. 44100 (Hz)
        cutoff_freq (float): filter cutoff frequency
        Q (float): https://en.wikipedia.org/wiki/Q_factor

    Returns:
        output_waveform (torch.Tensor): Dimension of `(channel, time)`
    """

    GAIN = 1.
    w0 = 2 * math.pi * cutoff_freq / sample_rate
    A = math.exp(GAIN / 40.0 * math.log(10))
    alpha = math.sin(w0) / 2 / Q
    mult = _dB2Linear(max(GAIN, 0))

    b0 = (1 - math.cos(w0)) / 2
    b1 = 1 - math.cos(w0)
    b2 = b0
    a0 = 1 + alpha
    a1 = -2 * math.cos(w0)
    a2 = 1 - alpha
    return biquad(waveform, b0, b1, b2, a0, a1, a2)


def equalizer_biquad(waveform, sample_rate, center_freq, gain, Q=0.707):
    # type: (Tensor, int, float, float, float) -> Tensor
    r"""Design biquad peaking equalizer filter and perform filtering.  Similar to SoX implementation.

    Args:
        waveform (torch.Tensor): audio waveform of dimension of `(channel, time)`
        sample_rate (int): sampling rate of the waveform, e.g. 44100 (Hz)
        center_freq (float): filter's central frequency
        gain (float): desired gain at the boost (or attenuation) in dB
        q_factor (float): https://en.wikipedia.org/wiki/Q_factor

    Returns:
        output_waveform (torch.Tensor): Dimension of `(channel, time)`
    """
    w0 = 2 * math.pi * center_freq / sample_rate
    A = math.exp(gain / 40.0 * math.log(10))
    alpha = math.sin(w0) / 2 / Q

    b0 = 1 + alpha * A
    b1 = -2 * math.cos(w0)
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * math.cos(w0)
    a2 = 1 - alpha / A
    return biquad(waveform, b0, b1, b2, a0, a1, a2)


def mask_along_axis_iid(specgrams, mask_param, mask_value, axis):
    # type: (Tensor, int, float, int) -> Tensor
    r"""
    Apply a mask along ``axis``. Mask will be applied from indices ``[v_0, v_0 + v)``, where
    ``v`` is sampled from ``uniform(0, mask_param)``, and ``v_0`` from ``uniform(0, max_v - v)``.
    All examples will have the same mask interval.

    Args:
        specgrams (Tensor): Real spectrograms (batch, channel, freq, time)
        mask_param (int): Number of columns to be masked will be uniformly sampled from [0, mask_param]
        mask_value (float): Value to assign to the masked columns
        axis (int): Axis to apply masking on (2 -> frequency, 3 -> time)

    Returns:
        torch.Tensor: Masked spectrograms of dimensions (batch, channel, freq, time)
    """

    if axis != 2 and axis != 3:
        raise ValueError('Only Frequency and Time masking are supported')

    value = torch.rand(specgrams.shape[:2]) * mask_param
    min_value = torch.rand(specgrams.shape[:2]) * (specgrams.size(axis) - value)

    # Create broadcastable mask
    mask_start = (min_value.long())[..., None, None].float()
    mask_end = (min_value.long() + value.long())[..., None, None].float()
    mask = torch.arange(0, specgrams.size(axis)).float()

    # Per batch example masking
    specgrams = specgrams.transpose(axis, -1)
    specgrams.masked_fill_((mask >= mask_start) & (mask < mask_end), mask_value)
    specgrams = specgrams.transpose(axis, -1)

    return specgrams


def mask_along_axis(specgram, mask_param, mask_value, axis):
    # type: (Tensor, int, float, int) -> Tensor
    r"""
    Apply a mask along ``axis``. Mask will be applied from indices ``[v_0, v_0 + v)``, where
    ``v`` is sampled from ``uniform(0, mask_param)``, and ``v_0`` from ``uniform(0, max_v - v)``.
    All examples will have the same mask interval.

    Args:
        specgram (Tensor): Real spectrogram (channel, freq, time)
        mask_param (int): Number of columns to be masked will be uniformly sampled from [0, mask_param]
        mask_value (float): Value to assign to the masked columns
        axis (int): Axis to apply masking on (1 -> frequency, 2 -> time)

    Returns:
        torch.Tensor: Masked spectrogram of dimensions (channel, freq, time)
    """

    # pack batch
    shape = specgram.size()
    specgram = specgram.reshape([-1] + list(shape[-2:]))

    value = torch.rand(1) * mask_param
    min_value = torch.rand(1) * (specgram.size(axis) - value)

    mask_start = (min_value.long()).squeeze()
    mask_end = (min_value.long() + value.long()).squeeze()

    assert mask_end - mask_start < mask_param
    if axis == 1:
        specgram[:, mask_start:mask_end] = mask_value
    elif axis == 2:
        specgram[:, :, mask_start:mask_end] = mask_value
    else:
        raise ValueError('Only Frequency and Time masking are supported')

    # unpack batch
    specgram = specgram.reshape(shape[:-2] + specgram.shape[-2:])

    return specgram.reshape(shape[:-2] + specgram.shape[-2:])


def compute_deltas(specgram, win_length=5, mode="replicate"):
    # type: (Tensor, int, str) -> Tensor
    r"""Compute delta coefficients of a tensor, usually a spectrogram:

    .. math::
        d_t = \frac{\sum_{n=1}^{\text{N}} n (c_{t+n} - c_{t-n})}{2 \sum_{n=1}^{\text{N} n^2}

    where :math:`d_t` is the deltas at time :math:`t`,
    :math:`c_t` is the spectrogram coeffcients at time :math:`t`,
    :math:`N` is (`win_length`-1)//2.

    Args:
        specgram (torch.Tensor): Tensor of audio of dimension (..., freq, time)
        win_length (int): The window length used for computing delta
        mode (str): Mode parameter passed to padding

    Returns:
        deltas (torch.Tensor): Tensor of audio of dimension (..., freq, time)

    Example
        >>> specgram = torch.randn(1, 40, 1000)
        >>> delta = compute_deltas(specgram)
        >>> delta2 = compute_deltas(delta)
    """

    # pack batch
    shape = specgram.size()
    specgram = specgram.reshape(1, -1, shape[-1])

    assert win_length >= 3

    n = (win_length - 1) // 2

    # twice sum of integer squared
    denom = n * (n + 1) * (2 * n + 1) / 3

    specgram = torch.nn.functional.pad(specgram, (n, n), mode=mode)

    kernel = (
        torch
        .arange(-n, n + 1, 1, device=specgram.device, dtype=specgram.dtype)
        .repeat(specgram.shape[1], 1, 1)
    )

    output = torch.nn.functional.conv1d(specgram, kernel, groups=specgram.shape[1]) / denom

    # unpack batch
    output = output.reshape(shape)

    return output


def gain(waveform, gain_db=1.0):
    # type: (Tensor, float) -> Tensor
    r"""Apply amplification or attenuation to the whole waveform.

    Args:
       waveform (torch.Tensor): Tensor of audio of dimension (channel, time).
       gain_db (float) Gain adjustment in decibels (dB) (Default: `1.0`).

    Returns:
       torch.Tensor: the whole waveform amplified by gain_db.
    """
    if (gain_db == 0):
        return waveform

    ratio = 10 ** (gain_db / 20)

    return waveform * ratio


def _add_noise_shaping(dithered_waveform, waveform):
    r"""Noise shaping is calculated by error:
    error[n] = dithered[n] - original[n]
    noise_shaped_waveform[n] = dithered[n] + error[n-1]
    """
    wf_shape = waveform.size()
    waveform = waveform.reshape(-1, wf_shape[-1])

    dithered_shape = dithered_waveform.size()
    dithered_waveform = dithered_waveform.reshape(-1, dithered_shape[-1])

    error = dithered_waveform - waveform

    # add error[n-1] to dithered_waveform[n], so offset the error by 1 index
    for index in range(error.size()[0]):
        err = error[index]
        error_offset = torch.cat((torch.zeros(1), err))
        error[index] = error_offset[:waveform.size()[1]]

    noise_shaped = dithered_waveform + error
    return noise_shaped.reshape(dithered_shape[:-1] + noise_shaped.shape[-1:])


def _apply_probability_distribution(waveform, density_function="TPDF"):
    # type: (Tensor, str) -> Tensor
    r"""Apply a probability distribution function on a waveform.

    Triangular probability density function (TPDF) dither noise has a
    triangular distribution; values in the center of the range have a higher
    probability of occurring.

    Rectangular probability density function (RPDF) dither noise has a
    uniform distribution; any value in the specified range has the same
    probability of occurring.

    Gaussian probability density function (GPDF) has a normal distribution.
    The relationship of probabilities of results follows a bell-shaped,
    or Gaussian curve, typical of dither generated by analog sources.
    Args:
        waveform (torch.Tensor): Tensor of audio of dimension (channel, time)
        probability_density_function (string): The density function of a
           continuous random variable (Default: `TPDF`)
           Options: Triangular Probability Density Function - `TPDF`
                    Rectangular Probability Density Function - `RPDF`
                    Gaussian Probability Density Function - `GPDF`
    Returns:
        torch.Tensor: waveform dithered with TPDF
    """
    shape = waveform.size()
    waveform = waveform.reshape(-1, shape[-1])

    channel_size = waveform.size()[0] - 1
    time_size = waveform.size()[-1] - 1

    random_channel = int(torch.randint(channel_size, [1, ]).item()) if channel_size > 0 else 0
    random_time = int(torch.randint(time_size, [1, ]).item()) if time_size > 0 else 0

    number_of_bits = 16
    up_scaling = 2 ** (number_of_bits - 1) - 2
    signal_scaled = waveform * up_scaling
    down_scaling = 2 ** (number_of_bits - 1)

    signal_scaled_dis = waveform
    if (density_function == "RPDF"):
        RPDF = waveform[random_channel][random_time] - 0.5

        signal_scaled_dis = signal_scaled + RPDF
    elif (density_function == "GPDF"):
        # TODO Replace by distribution code once
        # https://github.com/pytorch/pytorch/issues/29843 is resolved
        # gaussian = torch.distributions.normal.Normal(torch.mean(waveform, -1), 1).sample()

        num_rand_variables = 6

        gaussian = waveform[random_channel][random_time]
        for ws in num_rand_variables * [time_size]:
            rand_chan = int(torch.randint(channel_size, [1, ]).item())
            gaussian += waveform[rand_chan][int(torch.randint(ws, [1, ]).item())]

        signal_scaled_dis = signal_scaled + gaussian
    else:
        TPDF = torch.bartlett_window(time_size + 1)
        TPDF = TPDF.repeat((channel_size + 1), 1)
        signal_scaled_dis = signal_scaled + TPDF

    quantised_signal_scaled = torch.round(signal_scaled_dis)
    quantised_signal = quantised_signal_scaled / down_scaling
    return quantised_signal.reshape(shape[:-1] + quantised_signal.shape[-1:])


def dither(waveform, density_function="TPDF", noise_shaping=False):
    # type: (Tensor, str, bool) -> Tensor
    r"""Dither increases the perceived dynamic range of audio stored at a
    particular bit-depth by eliminating nonlinear truncation distortion
    (i.e. adding minimally perceived noise to mask distortion caused by quantization).
    Args:
       waveform (torch.Tensor): Tensor of audio of dimension (channel, time)
       density_function (string): The density function of a
           continuous random variable (Default: `TPDF`)
           Options: Triangular Probability Density Function - `TPDF`
                    Rectangular Probability Density Function - `RPDF`
                    Gaussian Probability Density Function - `GPDF`
       noise_shaping (boolean): a filtering process that shapes the spectral
           energy of quantisation error (Default: `False`)

    Returns:
       torch.Tensor: waveform dithered
    """
    dithered = _apply_probability_distribution(waveform, density_function=density_function)

    if noise_shaping:
        return _add_noise_shaping(dithered, waveform)
    else:
        return dithered


def _compute_nccf(waveform, sample_rate, frame_time, freq_low):
    # type: (Tensor, int, float, int) -> Tensor
    r"""
    Compute Normalized Cross-Correlation Function (NCCF).

    .. math::
        \phi_i(m) = \frac{\sum_{n=b_i}^{b_i + N-1} w(n) w(m+n)}{\sqrt{E(b_i) E(m+b_i)}},

    where
    :math:`\phi_i(m)` is the NCCF at frame :math:`i` with lag :math:`m`,
    :math:`w` is the waveform,
    :math:`N` is the lenght of a frame,
    :math:`b_i` is the beginning of frame :math:`i`,
    :math:`E(j)` is the energy :math:`\sum_{n=j}^{j+N-1} w^2(n)`.
    """

    EPSILON = 10 ** (-9)

    # Number of lags to check
    lags = math.ceil(sample_rate / freq_low)

    frame_size = int(math.ceil(sample_rate * frame_time))

    waveform_length = waveform.size()[-1]
    num_of_frames = math.ceil(waveform_length / frame_size)

    p = lags + num_of_frames * frame_size - waveform_length
    waveform = torch.nn.functional.pad(waveform, (0, p))

    # Compute lags
    output_lag = []
    for lag in range(1, lags + 1):
        s1 = waveform[..., :-lag].unfold(-1, frame_size, frame_size)[
            ..., :num_of_frames, :
        ]
        s2 = waveform[..., lag:].unfold(-1, frame_size, frame_size)[
            ..., :num_of_frames, :
        ]

        output_frames = (
            (s1 * s2).sum(-1)
            / (EPSILON + torch.norm(s1, p=2, dim=-1)).pow(2)
            / (EPSILON + torch.norm(s2, p=2, dim=-1)).pow(2)
        )

        output_lag.append(output_frames.unsqueeze(-1))

    nccf = torch.cat(output_lag, -1)

    return nccf


def _combine_max(a, b, thresh=0.99):
    # type: (Tuple[Tensor, Tensor], Tuple[Tensor, Tensor], float) -> Tuple[Tensor, Tensor]
    """
    Take value from first if bigger than a multiplicative factor of the second, elementwise.
    """
    mask = (a[0] > thresh * b[0])
    values = mask * a[0] + ~mask * b[0]
    indices = mask * a[1] + ~mask * b[1]
    return values, indices


def _find_max_per_frame(nccf, sample_rate, freq_high):
    # type: (Tensor, int, int) -> Tensor
    r"""
    For each frame, take the highest value of NCCF,
    apply centered median smoothing, and convert to frequency.

    Note: If the max among all the lags is very close
    to the first half of lags, then the latter is taken.
    """

    lag_min = math.ceil(sample_rate / freq_high)

    # Find near enough max that is smallest

    best = torch.max(nccf[..., lag_min:], -1)

    half_size = nccf.shape[-1] // 2
    half = torch.max(nccf[..., lag_min:half_size], -1)

    best = _combine_max(half, best)
    indices = best[1]

    # Add back minimal lag
    indices += lag_min
    # Add 1 empirical calibration offset
    indices += 1

    return indices


def _median_smoothing(indices, win_length):
    # type: (Tensor, int) -> Tensor
    r"""
    Apply median smoothing to the 1D tensor over the given window.
    """

    # Centered windowed
    pad_length = (win_length - 1) // 2

    # "replicate" padding in any dimension
    indices = torch.nn.functional.pad(
        indices, (pad_length, 0), mode="constant", value=0.
    )

    indices[..., :pad_length] = torch.cat(pad_length * [indices[..., pad_length].unsqueeze(-1)], dim=-1)
    roll = indices.unfold(-1, win_length, 1)

    values, _ = torch.median(roll, -1)
    return values


def detect_pitch_frequency(
    waveform,
    sample_rate,
    frame_time=10 ** (-2),
    win_length=30,
    freq_low=85,
    freq_high=3400,
):
    # type: (Tensor, int, float, int, int, int) -> Tensor
    r"""Detect pitch frequency.

    It is implemented using normalized cross-correlation function and median smoothing.

    Args:
        waveform (torch.Tensor): Tensor of audio of dimension (..., freq, time)
        sample_rate (int): The sample rate of the waveform (Hz)
        win_length (int): The window length for median smoothing (in number of frames)
        freq_low (int): Lowest frequency that can be detected (Hz)
        freq_high (int): Highest frequency that can be detected (Hz)

    Returns:
        freq (torch.Tensor): Tensor of audio of dimension (..., frame)
    """

    dim = waveform.dim()

    # pack batch
    shape = list(waveform.size())
    waveform = waveform.reshape([-1] + shape[-1:])

    nccf = _compute_nccf(waveform, sample_rate, frame_time, freq_low)
    indices = _find_max_per_frame(nccf, sample_rate, freq_high)
    indices = _median_smoothing(indices, win_length)

    # Convert indices to frequency
    EPSILON = 10 ** (-9)
    freq = sample_rate / (EPSILON + indices.to(torch.float))

    # unpack batch
    freq = freq.reshape(shape[:-1] + list(freq.shape[-1:]))

    return freq
