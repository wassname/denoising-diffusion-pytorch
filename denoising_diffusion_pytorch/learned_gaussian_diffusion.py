import torch
from math import pi, sqrt, log as ln
from inspect import isfunction
from torch import nn, einsum
from einops import rearrange

from denoising_diffusion_pytorch.denoising_diffusion_pytorch import GaussianDiffusion, extract, unnormalize_to_zero_to_one

# constants

NAT = 1. / ln(2)

# helper functions

def exists(x):
    return x is not None

def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d

# tensor helpers

def log(t, eps = 1e-12):
    return torch.log(t.clamp(min = eps))

def meanflat(x):
    return x.mean(dim = tuple(range(1, len(x.shape))))

def normal_kl(mean1, logvar1, mean2, logvar2):
    """
    KL divergence between normal distributions parameterized by mean and log-variance.
    """
    return 0.5 * (-1.0 + logvar2 - logvar1 + torch.exp(logvar1 - logvar2) + ((mean1 - mean2) ** 2) * torch.exp(-logvar2))

def approx_standard_normal_cdf(x):
    return 0.5 * (1.0 + torch.tanh(sqrt(2.0 / pi) * (x + 0.044715 * (x ** 3))))

def discretized_gaussian_log_likelihood(x, *, means, log_scales, thres = 0.999):
    assert x.shape == means.shape == log_scales.shape

    centered_x = x - means
    inv_stdv = torch.exp(-log_scales)
    plus_in = inv_stdv * (centered_x + 1. / 255.)
    cdf_plus = approx_standard_normal_cdf(plus_in)
    min_in = inv_stdv * (centered_x - 1. / 255.)
    cdf_min = approx_standard_normal_cdf(min_in)
    log_cdf_plus = log(cdf_plus)
    log_one_minus_cdf_min = log(1. - cdf_min)
    cdf_delta = cdf_plus - cdf_min

    log_probs = torch.where(x < -thres,
        log_cdf_plus,
        torch.where(x > thres,
            log_one_minus_cdf_min,
            log(cdf_delta)))

    return log_probs

# https://arxiv.org/abs/2102.09672

# i thought the results were questionable, if one were to focus only on FID
# but may as well get this in here for others to try, as GLIDE is using it (and DALL-E2 first stage of cascade)
# gaussian diffusion for learned variance + hybrid eps simple + vb loss

class LearnedGaussianDiffusion(GaussianDiffusion):
    def __init__(
        self,
        denoise_fn,
        vb_loss_weight = 0.001,  # lambda was 0.001 in the paper
        *args,
        **kwargs
    ):
        super().__init__(denoise_fn, *args, **kwargs)
        assert denoise_fn.out_dim == (denoise_fn.channels * 2), 'dimension out of unet must be twice the number of channels for learned variance - you can also set the `learned_variance` keyword argument on the Unet to be `True`'
        self.vb_loss_weight = vb_loss_weight

    def q_posterior_mean_variance(self, x_start, x_t, t):
        """
        Compute the mean and variance of the diffusion posterior q(x_{t-1} | x_t, x_0)
        """
        posterior_mean = (
            extract(self.posterior_mean_coef1, t, x_t.shape) * x_start +
            extract(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = extract(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract(self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def predict_xstart_from_xprev(self, x_t, t, xprev):
        # (xprev - coef2*x_t) / coef1
        return (
            extract(1. / self.posterior_mean_coef1, t, x_t.shape) * xprev -
            extract(self.posterior_mean_coef2 / self.posterior_mean_coef1, t, x_t.shape) * x_t
        )

    def p_mean_variance(self, *, x, t, clip_denoised, model_output = None):
        model_output = default(model_output, lambda: self.denoise_fn(x, t))
        pred_noise, var_interp_frac_unnormalized = model_output.chunk(2, dim = 1)

        min_log = extract(self.posterior_log_variance_clipped, t, x.shape)
        max_log = extract(torch.log(self.betas), t, x.shape)
        var_interp_frac = unnormalize_to_zero_to_one(var_interp_frac_unnormalized)

        model_log_variance = var_interp_frac * max_log + (1 - var_interp_frac) * min_log
        model_variance = model_log_variance.exp()

        x_start = self.predict_start_from_noise(x, t, pred_noise)
        model_mean, _, _ = self.q_posterior(x_start, x, t)

        return model_mean, model_variance, model_log_variance

    def p_losses(self, x_start, t, noise = None, clip_denoised = False):
        noise = default(noise, lambda: torch.randn_like(x_start))
        x_t = self.q_sample(x_start = x_start, t = t, noise = noise)

        # model output

        model_output = self.denoise_fn(x_t, t)

        # calculating kl loss for learned variance (interpolation)

        true_mean, _, true_log_variance_clipped = self.q_posterior_mean_variance(x_start = x_start, x_t = x_t, t = t)
        model_mean, _, model_log_variance = self.p_mean_variance(x = x_t, t = t, clip_denoised = clip_denoised, model_output = model_output)

        # kl loss with detached model predicted mean, for stability reasons as in paper

        kl = normal_kl(true_mean, true_log_variance_clipped, model_mean.detach(), model_log_variance)
        kl = meanflat(kl) * NAT

        decoder_nll = -discretized_gaussian_log_likelihood(x_start, means = model_mean, log_scales = 0.5 * model_log_variance)
        decoder_nll = meanflat(decoder_nll) * NAT

        # at the first timestep return the decoder NLL, otherwise return KL(q(x_{t-1}|x_t,x_0) || p(x_{t-1}|x_t))

        vb_losses = torch.where(t == 0, decoder_nll, kl)

        # simple loss - predicting noise, x0, or x_prev

        pred_noise, _ = model_output.chunk(2, dim = 1)

        simple_losses = self.loss_fn(pred_noise, noise)

        return simple_losses + vb_losses.mean() * self.vb_loss_weight