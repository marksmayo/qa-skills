import { createServer } from 'node:http';
import { readNote } from './notes.ts';

// planted: listen(port) with no host binds every interface, so anyone on the network can read notes
export function servePreview(port: number) {
  const server = createServer((request, response) => {
    const name = new URL(request.url ?? '/', 'http://preview.invalid').searchParams.get('note') ?? 'index.md';
    response.end(readNote(name));
  });
  server.listen(port);
  return server;
}
