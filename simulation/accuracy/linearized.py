import numpy as np
import scipy.linalg
import scipy.stats

import matrix

import util.logging
import util.parallel.universal
import util.parallel.with_multiprocessing

import simulation.accuracy.constants
import simulation.util.cache


class Base(simulation.util.cache.Cache):

    def __init__(self, measurements_object, model_options=None, model_job_options=None):
        base_dir = simulation.accuracy.constants.CACHE_DIRNAME

        if model_job_options is None:
            model_job_options = {}
        try:
            model_job_options['name']
        except KeyError:
            self.measurements = measurements_object
            model_job_options['name'] = f'A_{str(self)}'

        super().__init__(base_dir, measurements_object, model_options=model_options, model_job_options=model_job_options,
                         include_initial_concentrations_factor_to_model_parameters=True)
        self.dtype = np.float128

    def model_df(self):
        df = super().model_df(derivative_kind=None).astype(self.dtype)
        assert df.shape == (self.measurements.number_of_measurements, self.model_parameters_len)
        return df

    # *** uncertainty model parameters *** #

    @staticmethod
    def confidence_factor(alpha):
        assert 0 < alpha < 1
        return scipy.stats.norm.ppf((1 + alpha) / 2)

    def model_parameter_information_matrix_calculate(self, **kwargs):
        raise NotImplementedError("Please implement this method")

    def model_parameter_information_matrix(self, **kwargs):
        if len(kwargs):
            M = self.model_parameter_information_matrix_calculate(**kwargs)
        else:
            M = self._value_from_file_cache(simulation.accuracy.constants.INFORMATION_MATRIX_FILENAME,
                                            self.model_parameter_information_matrix_calculate)
        n = self.model_parameters_len
        assert M.shape == (n, n) and M.dtype == self.dtype
        return M

    def model_parameter_covariance_matrix_calculate(self, information_matrix=None):
        util.logging.debug('Calculating model parameter covariance matrix.')
        if information_matrix is None:
            information_matrix = self.model_parameter_information_matrix()
        else:
            information_matrix = np.asarray(information_matrix, dtype=self.dtype)
        covariance_matrix = scipy.linalg.inv(information_matrix)
        return covariance_matrix

    def model_parameter_covariance_matrix(self, information_matrix=None):
        if information_matrix is not None:
            return self.model_parameter_covariance_matrix_calculate(information_matrix=information_matrix)
        else:
            return self._value_from_file_cache(simulation.accuracy.constants.COVARIANCE_MATRIX_FILENAME,
                                               self.model_parameter_covariance_matrix_calculate)

    def model_parameter_correlation_matrix(self, information_matrix=None):
        util.logging.debug('Calculating model parameter correlation matrix.')
        covariance_matrix = self.model_parameter_covariance_matrix(information_matrix=information_matrix)
        inverse_derivatives = np.sqrt(covariance_matrix.diagonal())
        inverse_derivative_diagonal_marix = np.diag(inverse_derivatives)
        correlation_matrix = inverse_derivative_diagonal_marix @ covariance_matrix @ inverse_derivative_diagonal_marix
        return correlation_matrix

    def model_parameter_confidence_calculate(self, information_matrix=None, alpha=0.99, relative=True):
        assert 0 < alpha < 1
        util.logging.debug(f'Calculating model parameter confidence with confidence level {alpha} and relative {relative}.')
        covariance_matrix = self.model_parameter_covariance_matrix(information_matrix=information_matrix)
        diagonal = covariance_matrix.diagonal()
        gamma = self.confidence_factor(alpha)
        confidences = np.sqrt(diagonal) * gamma
        if relative:
            confidences /= self.model_parameters
        return confidences

    def model_parameter_confidence(self, information_matrix=None, alpha=0.99, relative=True):
        if information_matrix is not None:
            return self.model_parameter_confidence_calculate(information_matrix, alpha=alpha, relative=relative)
        else:
            return self._value_from_file_cache(simulation.accuracy.constants.PARAMETER_CONFIDENCE_FILENAME.format(alpha=alpha, relative=relative),
                                               lambda: self.model_parameter_confidence_calculate(alpha=alpha, relative=relative))

    def average_model_parameter_confidence(self, information_matrix=None, alpha=0.99, relative=True):
        util.logging.debug(f'Calculating average model parameter confidence with confidence level {alpha} and relative {relative}.')
        return self.model_parameter_confidence(information_matrix=information_matrix, alpha=alpha, relative=relative).mean(dtype=self.dtype)

    # *** uncertainty in model output *** #

    def model_confidence_calculate_for_index(self, confidence_index, model_parameter_covariance_matrix, df_all, time_step_size, gamma, mask_is_sea, value_mask=None):
        if mask_is_sea[confidence_index[2:]] and (value_mask is None or value_mask[confidence_index]):
            time_index_start = confidence_index[1] * time_step_size
            # average
            confidence = 0.0
            for time_index_offset in range(time_step_size):
                df_i = df_all[confidence_index[0]][time_index_start + time_index_offset][confidence_index[2:]]
                confidence += np.sqrt(df_i @ model_parameter_covariance_matrix @ df_i)
            confidence /= time_step_size
            # mutiply with confidence factor
            confidence *= gamma
        else:
            confidence = np.nan
        return confidence

    def model_confidence_calculate(self, information_matrix=None, alpha=0.99, time_dim_confidence=12, time_dim_model=2880, parallel=True):
        util.logging.debug(f'Calculating model confidence with confidence level {alpha}, desired time dim {time_dim_confidence} of the confidence and time dim {time_dim_model}.')

        # calculate needed values
        if time_dim_model % time_dim_confidence == 0:
            time_step_size = int(time_dim_model / time_dim_confidence)
        else:
            raise ValueError(f'The desired time dimension {time_dim_confidence} of the confidence can not be satisfied because the time dimension of the model {time_dim_model} is not divisible by {time_dim_confidence}.')

        model_parameter_covariance_matrix = self.model_parameter_covariance_matrix(information_matrix=information_matrix)
        gamma = self.confidence_factor(alpha)
        df_all = self.model_df_all_boxes(time_dim_model)
        mask_is_sea = ~ np.isnan(df_all[0, 0, :, :, :, 0])
        confidence_shape = (df_all.shape[0], time_dim_confidence) + df_all.shape[2:-1]

        # prepare parallel execution
        if parallel:
            df_all = util.parallel.with_multiprocessing.shared_array(df_all)
            mask_is_sea = util.parallel.with_multiprocessing.shared_array(mask_is_sea)
            parallel_mode = util.parallel.universal.MODES['multiprocessing']
            chunksize = np.sort(confidence_shape)[-2:].prod()
        else:
            parallel_mode = util.parallel.universal.MODES['serial']
            chunksize = None

        # calculate confidence
        confidence = util.parallel.universal.create_array(confidence_shape, self.model_confidence_calculate_for_index, model_parameter_covariance_matrix, df_all, time_step_size, gamma, mask_is_sea, parallel_mode=parallel_mode, chunksize=chunksize)

        return confidence

    def model_confidence(self, information_matrix=None, alpha=0.99, time_dim_confidence=12, time_dim_model=2880, parallel=True):
        if information_matrix is not None:
            return self.model_confidence_calculate(information_matrix=information_matrix, alpha=alpha,
                                                   time_dim_confidence=time_dim_confidence, time_dim_model=time_dim_model,
                                                   parallel=parallel)
        else:
            return self._value_from_file_cache(simulation.accuracy.constants.MODEL_CONFIDENCE_FILENAME.format(
                                               alpha=alpha, time_dim_confidence=time_dim_confidence, time_dim_model=time_dim_model),
                                               lambda: self.model_confidence_calculate(
                                               alpha=alpha, time_dim_confidence=time_dim_confidence, time_dim_model=time_dim_model, parallel=parallel),
                                               save_as_txt=False, save_as_np=True)

    def average_model_confidence_calculate(self, information_matrix=None, alpha=0.99, time_dim_model=2880, relative=True, parallel=True):
        util.logging.debug(f'Calculating average model output confidence with confidence level {alpha}, relative {relative} and model time dim {time_dim_model}.')
        # model confidence
        if information_matrix is None:
            time_dim_confidence = 12
        else:
            time_dim_confidence = 1
        model_confidence = self.model_confidence(information_matrix=information_matrix, alpha=alpha,
                                                 time_dim_confidence=time_dim_confidence, time_dim_model=time_dim_model,
                                                 parallel=parallel)
        # averaging
        model_confidence = np.nanmean(model_confidence, axis=tuple(range(1, model_confidence.ndim)), dtype=self.dtype)
        if relative:
            model_output = self.model_f_all_boxes(time_dim_model)
            model_output = np.nanmean(model_output, axis=tuple(range(1, model_output.ndim)), dtype=self.dtype)
            model_confidence = model_confidence / model_output
        model_confidence = np.mean(model_confidence, dtype=self.dtype)

        util.logging.debug(f'Average model confidence {model_confidence} calculated for confidence level {alpha} and model time dim {time_dim_model} using relative values {relative}.')
        return model_confidence

    def average_model_confidence(self, information_matrix=None, alpha=0.99, time_dim_model=2880, relative=True, parallel=True):
        if information_matrix is not None:
            return self.average_model_confidence_calculate(information_matrix=information_matrix, alpha=alpha,
                                                           time_dim_model=time_dim_model, relative=relative,
                                                           parallel=parallel)
        else:
            return self._value_from_file_cache(simulation.accuracy.constants.AVERAGE_MODEL_CONFIDENCE_FILENAME.format(
                                               alpha=alpha, time_dim_model=time_dim_model, relative=relative),
                                               lambda: self.average_model_confidence_calculate(
                                               alpha=alpha, time_dim_model=time_dim_model, relative=relative,
                                               parallel=parallel))

    def average_model_confidence_increase_calculate(self, number_of_measurements=1, alpha=0.99, time_dim_confidence_increase=12, time_dim_model=2880, relative=True, parallel=True):
        util.logging.debug(f'Calculating average model output confidence increase with confidence level {alpha}, relative {relative}, model time dim {time_dim_model}, condifence time dim {time_dim_confidence_increase} and number_of_measurements {number_of_measurements}.')

        # get lengths
        k = self.measurements.number_of_measurements
        m = k + number_of_measurements
        n = self.model.model_options.parameters_len

        # make extended df
        df_points = np.empty((m, n), dtype=self.dtype)
        df_points[:k] = self.model_df()
        df_all = self.model_df_all_boxes(time_dim_model)

        # make extended standard deviation vector
        standard_deviations = np.empty(m, dtype=self.dtype)
        standard_deviations[:k] = self.measurements.standard_deviations
        standard_deviations_for_sample_lsm = self.measurements.standard_deviations_for_sample_lsm[index_measurements]

        # make extended correlation matrix
        correlations_matrix = self.measurements.correlations_own()
        correlations_matrix = correlations_matrix.reshape((m, m), copy=False)
        for i in range(k, m):
            correlations_matrix[i, i] = 1

        # make average_model_confidence_increase array
        average_model_confidence_increase_shape = (df_all.shape[0], time_dim_confidence_increase) + df_all.shape[2:-1]
        average_model_confidence_increase = np.empty(average_model_confidence_increase_shape, dtype=self.dtype)

        # change time dim in model lsm
        model_lsm = self.model.model_lsm
        old_time_dim_model = model_lsm.t_dim
        model_lsm.t_dim = time_dim_model

        # calculate new average_model_confidence for each index
        for index_model in np.ndindex(*average_model_confidence_increase_shape):
            df_at_index = df_all[index_model]
            util.logging.debug(f'Calculating average model output confidence increase for index {index_model} with confidecne shape {average_model_confidence_increase_shape}.')
            if not np.any(np.isnan(df_at_index)):
                # update df
                df_points[n:k] = df_at_index
                # update standard deviations
                coordinate = model_lsm.map_index_to_coordinate(*index_model)
                index_measurements = self.measurements.sample_lsm.coordinate_to_map_index(*coordinate, discard_year=True)
                standard_deviations[n:k] = standard_deviations_for_sample_lsm[index_measurements]
                # calculate confidence
                information_matrix = self.model_parameter_information_matrix(df=df_points, standard_deviations=standard_deviations, correlations_matrix=correlations_matrix)
                confidence_at_index = self.average_model_confidence(information_matrix=information_matrix, alpha=alpha, time_dim_model=time_dim_model, relative=relative, parallel=parallel)
            else:
                confidence_at_index = np.nan
            average_model_confidence_increase[index_model] = confidence_at_index

        # restore time dim in model lsm
        model_lsm.t_dim = old_time_dim_model

        # claculate increase of confidence
        average_model_confidence = self.average_model_confidence(alpha=alpha, time_dim_model=time_dim_model, relative=relative, parallel=parallel)
        average_model_confidence_increase = average_model_confidence - average_model_confidence_increase
        return average_model_confidence_increase

    def average_model_confidence_increase(self, number_of_measurements=1, alpha=0.99, time_dim_confidence_increase=12, time_dim_model=2880, relative=True, parallel=True):
        return self._value_from_file_cache(simulation.accuracy.constants.AVERAGE_MODEL_CONFIDENCE_INCREASE_FILENAME.format(
                                           number_of_measurements=number_of_measurements, alpha=alpha, relative=relative,
                                           time_dim_confidence_increase=time_dim_confidence_increase, time_dim_model=time_dim_model),
                                           lambda: self.average_model_confidence_increase_calculate(
                                           number_of_measurements=number_of_measurements, alpha=alpha, relative=relative,
                                           time_dim_confidence_increase=time_dim_confidence_increase, time_dim_model=time_dim_model, parallel=parallel),
                                           save_as_txt=False, save_as_np=True)


class OLS(Base):

    def model_parameter_information_matrix_calculate(self, df=None):
        # prepare df
        if df is None:
            df = self.model_df().astype(self.dtype)
        else:
            df = np.asarray(df, dtype=self.dtype)
        assert df.ndim == 2
        util.logging.debug(f'Calculating information matrix of type {self.name} with df {df.shape}.')
        # calculate matrix
        average_standard_deviation = self.measurements.standard_deviations.mean(dtype=self.dtype)
        M = df.T @ df
        M *= (average_standard_deviation)**-2
        return M

    def model_parameter_information_matrix(self, **kwargs):
        M = super().model_parameter_information_matrix()
        if len(kwargs) > 0:
            M += self.model_parameter_information_matrix_calculate(df=kwargs['df'])


class WLS(Base):

    def model_parameter_information_matrix_calculate(self, df=None, standard_deviations=None):
        # prepare df and standard deviations
        if df is None:
            df = self.model_df().astype(self.dtype)
            standard_deviations = self.measurements.standard_deviations
        else:
            assert standard_deviations is not None
            df = np.asarray(df, dtype=self.dtype)
            standard_deviations = np.asarray(standard_deviations, dtype=self.dtype)
        assert df.ndim == 2
        assert standard_deviations.ndim == 1
        assert len(df) == len(standard_deviations)
        # calculate matrix
        util.logging.debug(f'Calculating information matrix of type {self.name} with df {df.shape}.')
        weighted_df = df * standard_deviations[:, np.newaxis]**-1
        M = weighted_df.T @ weighted_df
        return M

    def model_parameter_information_matrix(self, **kwargs):
        M = super().model_parameter_information_matrix()
        if len(kwargs) > 0:
            M += self.model_parameter_information_matrix_calculate(df=kwargs['df'], standard_deviations=kwargs['standard_deviations'])


class GLS(Base):

    def model_parameter_information_matrix_calculate(self, df=None, standard_deviations=None, correlation_matrix=None, correlation_matrix_decomposition=None):
        # prepare df and standard deviations and correlation matrix decomposition
        if df is None:
            df = self.model_df().astype(self.dtype)
            standard_deviations = self.measurements.standard_deviations
            correlation_matrix_decomposition = self.measurements.correlations_own_decomposition
        else:
            assert standard_deviations is not None
            assert correlation_matrix is not None or correlation_matrix_decomposition is not None
            df = np.asarray(df, dtype=self.dtype)
            standard_deviations = np.asarray(standard_deviations, dtype=self.dtype)
            if correlation_matrix_decomposition is None:
                correlation_matrix = np.asarray(correlation_matrix, dtype=self.dtype)
                assert correlation_matrix.ndim == 2
                assert correlation_matrix.shape[0] == correlation_matrix.shape[1]
                assert correlation_matrix.shape[0] == standard_deviations.shape[0]
                correlation_matrix_decomposition = matrix.decompose(correlation_matrix, return_type=matrix.LDL_DECOMPOSITION_TYPE)
        assert df.ndim == 2
        assert standard_deviations.ndim == 1
        assert len(df) == len(standard_deviations)
        # calculate matrix
        util.logging.debug(f'Calculating information matrix of type {self.name} with df {df.shape}.')
        weighted_df = df / standard_deviations[:, np.newaxis]
        M = correlation_matrix_decomposition.inverse_matrix_both_sides_multiplication(weighted_df)
        return M