import type { Readable, Writable } from 'node:stream';
import { fork } from 'node:child_process';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { readNote } from './notes.ts';

export async function serveMcp(input: Readable, output: Writable): Promise<void> {
  const server = new McpServer({ name: 'quill', version: '0.4.0' });
  server.registerTool(
    'read_note',
    { description: 'Read a note by its path' },
    async (args: { path: string }) => ({ content: [{ type: 'text', text: readNote(args.path) }] }),
  );
  await server.connect(new StdioServerTransport(input, output));
}

/** Starts the indexer module the settings name. */
export function startIndexer(settings: { indexer: string }) {
  return fork(settings.indexer, [], { stdio: 'ignore' });
}
