import { echoRequestFields } from '../responses/responseTransform';

describe('echoRequestFields', () => {
  it('drops credentials from non-function tools but keeps the rest', () => {
    const echoed = echoRequestFields({
      tools: [
        {
          type: 'mcp',
          server_label: 'docs',
          server_url: 'https://mcp.example.com',
          headers: { Authorization: 'Bearer secret-token' },
          authorization: 'secret-token',
          require_approval: 'never',
        },
        { type: 'web_search' },
      ],
    });
    expect(echoed.tools).toEqual([
      {
        type: 'mcp',
        server_label: 'docs',
        server_url: 'https://mcp.example.com',
        require_approval: 'never',
      },
      { type: 'web_search' },
    ]);
    expect(JSON.stringify(echoed)).not.toContain('secret-token');
  });
});
