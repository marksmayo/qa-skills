import { createServer } from 'node:http';

const host = '127.0.0.1';

export const oauthSettings = {
  tokenPath: '/oauth/token',
  authorizePath: '/oauth/authorize',
};

/** Waits for the browser to come back from sign-in on a loopback port. */
export function waitForCallback(port: number, state: string): Promise<string | undefined> {
  return new Promise((resolve) => {
    const server = createServer((request, response) => {
      const url = new URL(request.url ?? '/', `http://${host}:${port}`);
      if (url.searchParams.get('state') !== state) {
        response.writeHead(400).end('state mismatch');
        return resolve(undefined);
      }
      response.writeHead(200).end('Signed in');
      resolve(url.searchParams.get('code') ?? undefined);
    });
    server.listen(port, host);
  });
}
