// Discord capture CLI: one JSON object per observed table-channel message.
//
// Gateway connection state lives in gateway.mjs so it can be exercised with
// deterministic sockets and clocks. Stdout remains message-only for the
// Python ingester; typed connection/capability diagnostics go to stderr.
//
// This listener is deliberately live-only until a downstream commit handshake
// exists. It does not persist a receive cursor, claim cold-start backfill, or
// pretend REST can recover fields hidden by the MESSAGE_CONTENT intent.

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { DiscordGatewayCapture } from './gateway.mjs';

const cfgPath = resolve(process.argv[2] || process.env.TABLE_CONFIG || 'table.json');
const cfg = JSON.parse(readFileSync(cfgPath, 'utf8'));
const transport = cfg.transport || {};
const channelId = transport.channel_id;
const tokenEnv = transport.token_env || 'TABLE_BOT_TOKEN';
const token = process.env[tokenEnv];

if (!channelId) {
  console.error(`[listen] transport.channel_id missing from ${cfgPath}`);
  process.exit(2);
}
if (!token) {
  console.error(`[listen] $${tokenEnv} is not set (tokens are never read from config)`);
  process.exit(2);
}

function emit(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function diagnostic(event) {
  process.stderr.write(`[listen] ${JSON.stringify(event)}\n`);
}

const capture = new DiscordGatewayCapture({
  token,
  channelId,
  onMessage: emit,
  onDiagnostic: diagnostic,
  onFatal: () => { process.exitCode = 2; },
});

capture.start();

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => capture.stop());
}
