// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { isSafeUpstreamURL } from '.';

describe('isSafeUpstreamURL', () => {
  test.each([
    'https://api.openai.com/v1',
    'https://myresource.openai.azure.com/openai',
    'https://bedrock-runtime.us-east-1.amazonaws.com',
    'https://us-central1-aiplatform.googleapis.com/v1',
    'http://localhost:11434',
    'http://[::1]:8080', // trusted by default for local development
  ])('allows %s', (url) => {
    expect(isSafeUpstreamURL(url)).toBe(true);
  });

  test.each([
    'http://169.254.169.254/latest/meta-data',
    'https://169.254.169.254#.openai.azure.com',
    'http://10.0.0.5:8080',
    'http://192.168.1.1',
    'http://[fe80::1]:8080',
    'http://[fd00:ec2::254]',
    'https://[::ffff:169.254.169.254]/',
    'https://[::ffff:a9fe:a9fe]/',
    'https://[::ffff:7f00:1]/',
    'https://[64:ff9b::a9fe:a9fe]/',
    'https://[2002:a9fe:a9fe::]/',
    'https://[::169.254.169.254]/',
    'https://[2606:4700::1111]/',
    'http://167772165', // 10.0.0.5 in decimal
    'http://metadata.google.internal/computeMetadata',
    'http://user:pass@api.openai.com',
    'file:///etc/passwd',
    'gopher://example.com',
  ])('blocks %s', (url) => {
    expect(isSafeUpstreamURL(url)).toBe(false);
  });

  test('an injected Azure resource name cannot reach the metadata service', () => {
    const base = `https://${'169.254.169.254/latest/meta-data#'}.openai.azure.com`;
    expect(isSafeUpstreamURL(base)).toBe(false);
  });
});
