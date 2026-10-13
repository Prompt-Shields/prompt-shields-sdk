import { GatewayError } from '../../errors/GatewayError';
import { awsEndpointDomain, isValidAwsRegion } from '../../utils/awsAuth';
import {
  generateAWSHeaders,
  providerAssumedRoleCredentials,
} from '../bedrock/utils';
import { ProviderAPIConfig } from '../types';
import { env } from 'hono/adapter';
const SagemakerAPIConfig: ProviderAPIConfig = {
  getBaseURL: ({ providerOptions }) => {
    if (!isValidAwsRegion(providerOptions.awsRegion)) {
      throw new GatewayError('Invalid AWS region', 400);
    }
    return `https://runtime.sagemaker.${providerOptions.awsRegion}.${awsEndpointDomain}`;
  },
  headers: async ({
    providerOptions,
    transformedRequestBody,
    transformedRequestUrl,
    c,
  }) => {
    const headers: Record<string, string> = {
      'content-type': 'application/json',
    };

    await providerAssumedRoleCredentials(providerOptions, env(c));

    const awsHeaders = await generateAWSHeaders(
      transformedRequestBody,
      headers,
      transformedRequestUrl,
      'POST',
      'sagemaker',
      providerOptions.awsRegion || '',
      providerOptions.awsAccessKeyId || '',
      providerOptions.awsSecretAccessKey || '',
      providerOptions.awsSessionToken || ''
    );

    if (providerOptions.amznSagemakerCustomAttributes) {
      awsHeaders['x-amzn-sagemaker-custom-attributes'] =
        providerOptions.amznSagemakerCustomAttributes;
    }

    if (providerOptions.amznSagemakerTargetModel) {
      awsHeaders['x-amzn-sagemaker-target-model'] =
        providerOptions.amznSagemakerTargetModel;
    }

    if (providerOptions.amznSagemakerTargetVariant) {
      awsHeaders['x-amzn-sagemaker-target-variant'] =
        providerOptions.amznSagemakerTargetVariant;
    }

    if (providerOptions.amznSagemakerTargetContainerHostname) {
      awsHeaders['x-amzn-sagemaker-target-container-hostname'] =
        providerOptions.amznSagemakerTargetContainerHostname;
    }

    if (providerOptions.amznSagemakerInferenceId) {
      awsHeaders['x-amzn-sagemaker-inference-id'] =
        providerOptions.amznSagemakerInferenceId;
    }

    if (providerOptions.amznSagemakerEnableExplanations) {
      awsHeaders['x-amzn-sagemaker-enable-explanations'] =
        providerOptions.amznSagemakerEnableExplanations;
    }

    if (providerOptions.amznSagemakerInferenceComponent) {
      awsHeaders['x-amzn-sagemaker-inference-component'] =
        providerOptions.amznSagemakerInferenceComponent;
    }

    if (providerOptions.amznSagemakerSessionId) {
      awsHeaders['x-amzn-sagemaker-session-id'] =
        providerOptions.amznSagemakerSessionId;
    }
    return awsHeaders;
  },
  getEndpoint: ({ gatewayRequestURL }) => gatewayRequestURL.split('/v1')[1],
};

export default SagemakerAPIConfig;
