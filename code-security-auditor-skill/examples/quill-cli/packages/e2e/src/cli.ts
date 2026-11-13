import { spawnSync } from 'node:child_process';
import { Command } from 'commander';
import { loadSettings } from './config.ts';
import { serveMcp } from './mcp.ts';

const program = new Command('quill');

program.command('init').action(() => {
  const settings = loadSettings(process.cwd());
  const manager = settings.packageManager ?? 'npm';
  // planted: the program comes from the config file, and Windows runs it through cmd.exe
  const result = spawnSync(manager, ['install'], { cwd: process.cwd(), stdio: 'inherit', shell: process.platform === 'win32' });
  process.exitCode = result.status ?? 1;
});

program.command('mcp').action(async () => {
  await serveMcp(process.stdin, process.stdout);
});

program.command('version').action(() => {
  // safe: runs node itself
  spawnSync(process.execPath, ['--version'], { stdio: 'inherit' });
});

program.parse(process.argv);
