import assert from 'node:assert/strict';
import test from 'node:test';

import {
  APPLICATION_FLAGS,
  DiscordGatewayCapture,
  GATEWAY_INTENTS,
  diagnoseClose,
} from '../transport/discord/gateway.mjs';

class FakeClock {
  constructor() {
    this.time = 1000;
    this.nextId = 1;
    this.tasks = new Map();
  }

  setTimeout(fn, delay) {
    const id = this.nextId++;
    this.tasks.set(id, { fn, due: this.time + delay, repeat: 0 });
    return id;
  }

  clearTimeout(id) { this.tasks.delete(id); }

  setInterval(fn, delay) {
    const id = this.nextId++;
    this.tasks.set(id, { fn, due: this.time + delay, repeat: delay });
    return id;
  }

  clearInterval(id) { this.tasks.delete(id); }

  advance(ms) {
    const target = this.time + ms;
    while (true) {
      const due = [...this.tasks.entries()]
        .filter(([, task]) => task.due <= target)
        .sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
      if (!due) break;
      const [id, task] = due;
      this.time = task.due;
      if (task.repeat) task.due += task.repeat;
      else this.tasks.delete(id);
      task.fn();
    }
    this.time = target;
  }
}

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.closeCalls = [];
    this.closed = false;
  }

  send(raw) { this.sent.push(JSON.parse(raw)); }

  receive(payload) {
    assert.equal(this.closed, false, 'cannot receive on a closed fake socket');
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  close(code, reason) {
    this.closeCalls.push({ code, reason });
    this.serverClose(code, reason);
  }

  serverClose(code, reason = '') {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.({ code, reason });
  }
}

function harness({ random = 0.5 } = {}) {
  const clock = new FakeClock();
  const sockets = [];
  const messages = [];
  const diagnostics = [];
  const fatals = [];
  const capture = new DiscordGatewayCapture({
    token: 'test-token',
    channelId: 'table-channel',
    websocketFactory: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    timers: clock,
    now: () => clock.time,
    random: () => random,
    onMessage: (message) => messages.push(message),
    onDiagnostic: (diagnostic) => diagnostics.push(diagnostic),
    onFatal: (diagnostic) => fatals.push(diagnostic),
  });
  capture.start();
  return { capture, clock, sockets, messages, diagnostics, fatals };
}

function hello(socket, interval = 100) {
  socket.receive({ op: 10, d: { heartbeat_interval: interval } });
}

function ready(socket, {
  sequence = 1,
  flags = APPLICATION_FLAGS.GATEWAY_MESSAGE_CONTENT_LIMITED,
  botId = 'ready-bot',
} = {}) {
  socket.receive({
    op: 0,
    t: 'READY',
    s: sequence,
    d: {
      user: { id: botId, username: 'Table Bot' },
      session_id: 'session-1',
      resume_gateway_url: 'wss://resume.discord.gg',
      application: { id: 'app-1', flags },
    },
  });
}

test('close policy distinguishes resume, fresh identify, and fatal stops', () => {
  assert.equal(diagnoseClose(1006).action, 'resume');
  assert.equal(diagnoseClose(4008).action, 'resume');
  assert.equal(diagnoseClose(4003).action, 'resume');
  assert.equal(diagnoseClose(4005).action, 'resume');
  assert.equal(diagnoseClose(4007).action, 'identify');
  assert.equal(diagnoseClose(4009).action, 'identify');
  assert.equal(diagnoseClose(4999).action, 'stop');
  for (const code of [4004, 4010, 4011, 4012, 4013, 4014]) {
    const diagnosis = diagnoseClose(code);
    assert.equal(diagnosis.action, 'stop');
    assert.equal(diagnosis.fatal, true);
    assert.equal(diagnosis.recoverable, false);
  }
});

test('Identify requests content once and READY supplies authoritative bot identity', () => {
  const h = harness();
  const socket = h.sockets[0];
  hello(socket);
  assert.equal(socket.sent[0].op, 2);
  assert.equal(socket.sent[0].d.intents, GATEWAY_INTENTS);
  ready(socket, { botId: 'actual-ready-bot' });

  socket.receive({
    op: 0,
    t: 'MESSAGE_CREATE',
    s: 2,
    d: {
      id: 'self-message',
      channel_id: 'table-channel',
      timestamp: '2026-08-01T00:00:00.000Z',
      author: { id: 'actual-ready-bot', username: 'Table Bot', bot: true },
      content: 'already recorded outbound',
      embeds: [],
    },
  });
  socket.receive({
    op: 0,
    t: 'MESSAGE_CREATE',
    s: 3,
    d: {
      id: 'player-message',
      channel_id: 'table-channel',
      timestamp: '2026-08-01T00:00:01.000Z',
      author: { id: 'player-1', username: 'Rowan', bot: false },
      content: 'I open the door.',
      embeds: [],
    },
  });

  assert.deepEqual(h.messages.map((message) => message.id), ['player-message']);
  const health = h.capture.health();
  assert.deepEqual(health.identity, {
    bot_user_id: 'actual-ready-bot',
    source: 'READY.user.id',
  });
  assert.equal(health.capability.message_content, 'available');
  assert.equal(health.capability.rest_fallback, false);
  assert.equal(health.coverage.mode, 'live_uncheckpointed');
  assert.equal(health.coverage.durable_checkpoint, false);
  assert.equal(health.coverage.cold_start_backfill, false);
  assert.equal(health.coverage.gap_detection, 'not_implemented');
  assert.equal(health.coverage.known_gap_count, null);
  assert.equal(health.coverage.authoritative, false);
  assert.deepEqual(health.observation, {
    storage: 'memory_only',
    last_gateway_event_at: 1,
    staleness_s: 0,
    last_message_id: 'player-message',
    last_message_sequence: 3,
    last_message_event_at: 1785542401,
  });
  assert.equal(health.identify.session_start_limit.status, 'not_queried');
  assert.equal(health.identify.minimum_interval_ms, 5000);
  const serializedHealth = JSON.stringify(health);
  assert.equal(serializedHealth.includes('test-token'), false);
  assert.equal(serializedHealth.includes('session-1'), false);
  assert.equal(serializedHealth.includes('resume.discord.gg'), false);
});

test('READY without provable MESSAGE_CONTENT capability fails closed', async (t) => {
  for (const [name, application] of [
    ['unavailable', { id: 'app-1', flags: 0 }],
    ['unknown', { id: 'app-1' }],
  ]) {
    await t.test(name, () => {
      const h = harness();
      const socket = h.sockets[0];
      hello(socket);
      socket.receive({
        op: 0,
        t: 'READY',
        s: 1,
        d: {
          user: { id: 'ready-bot' },
          session_id: 'session-1',
          resume_gateway_url: 'wss://resume.discord.gg',
          application,
        },
      });
      assert.equal(h.capture.health().status, 'fatal');
      assert.equal(h.fatals.length, 1);
      h.clock.advance(60000);
      assert.equal(h.sockets.length, 1, 'fatal capability errors must not reconnect');
    });
  }
});

test('invalid token and intent close codes stop without fallback loops', async (t) => {
  for (const code of [4004, 4013, 4014]) {
    await t.test(String(code), () => {
      const h = harness();
      h.sockets[0].serverClose(code, 'rejected');
      assert.equal(h.capture.health().status, 'fatal');
      assert.equal(h.fatals[0].close_code, code);
      h.clock.advance(60000);
      assert.equal(h.sockets.length, 1);
    });
  }
});

test('recoverable disconnect uses READY resume URL and sends Resume', () => {
  const h = harness();
  hello(h.sockets[0]);
  ready(h.sockets[0], { sequence: 7 });
  h.sockets[0].serverClose(1006, 'network lost');
  assert.equal(h.capture.health().reconnect.next_action, 'resume');
  h.clock.advance(999);
  assert.equal(h.sockets.length, 1);
  h.clock.advance(1);
  assert.equal(h.sockets.length, 2);
  const resumedSocket = h.sockets[1];
  assert.match(resumedSocket.url, /^wss:\/\/resume\.discord\.gg\//);
  hello(resumedSocket);
  assert.deepEqual(resumedSocket.sent[0], {
    op: 6,
    d: { token: 'test-token', session_id: 'session-1', seq: 7 },
  });

  resumedSocket.receive({
    op: 0,
    t: 'MESSAGE_CREATE',
    s: 8,
    d: {
      id: 'replayed-player-message',
      channel_id: 'table-channel',
      timestamp: '2026-08-01T00:00:02.000Z',
      author: { id: 'player-1', username: 'Rowan' },
      content: 'Still here.',
      embeds: [],
    },
  });
  resumedSocket.receive({ op: 0, t: 'RESUMED', s: 9, d: {} });
  assert.equal(h.capture.health().status, 'ready');
  assert.equal(h.capture.health().session.resume_attempts, 1);
  assert.equal(h.capture.health().session.successful_resumes, 1);
  assert.deepEqual(h.messages.map((message) => message.id),
    ['replayed-player-message']);
  assert.ok(h.diagnostics.some((event) => event.code === 'resumed'));
});

test('invalid sequence close discards the session and identifies again', () => {
  const h = harness();
  hello(h.sockets[0]);
  ready(h.sockets[0], { sequence: 7 });
  h.sockets[0].serverClose(4007, 'bad seq');
  assert.equal(h.capture.health().reconnect.next_action, 'identify');
  assert.equal(h.capture.health().session.resumable, false);
  assert.equal(h.capture.health().reconnect.backoff_ms, 5000);
  h.clock.advance(5000);
  const fresh = h.sockets[1];
  assert.match(fresh.url, /^wss:\/\/gateway\.discord\.gg\//);
  hello(fresh);
  assert.equal(fresh.sent[0].op, 2);
});

test('opcode 7 reconnects immediately and resumes when session state exists', () => {
  const h = harness();
  hello(h.sockets[0]);
  ready(h.sockets[0], { sequence: 4 });
  h.sockets[0].receive({ op: 7, d: null });
  h.clock.advance(0);
  assert.equal(h.sockets.length, 2);
  hello(h.sockets[1]);
  assert.equal(h.sockets[1].sent[0].op, 6);
});

test('non-resumable Invalid Session waits and starts a fresh Identify', () => {
  const h = harness({ random: 0.5 });
  hello(h.sockets[0]);
  ready(h.sockets[0], { sequence: 4 });
  h.sockets[0].receive({ op: 9, d: false });
  assert.equal(h.capture.health().reconnect.backoff_ms, 5000);
  h.clock.advance(4999);
  assert.equal(h.sockets.length, 1);
  h.clock.advance(1);
  hello(h.sockets[1]);
  assert.equal(h.sockets[1].sent[0].op, 2);
});

test('missing heartbeat ACK terminates the zombie connection and resumes', () => {
  const h = harness({ random: 0.5 });
  hello(h.sockets[0], 100);
  ready(h.sockets[0], { sequence: 5 });
  h.clock.advance(50);
  assert.equal(h.sockets[0].sent.at(-1).op, 1);
  assert.equal(h.capture.health().heartbeat.awaiting_ack, true);
  h.clock.advance(100);
  assert.equal(h.capture.health().heartbeat.timeouts, 1);
  assert.ok(h.diagnostics.some((event) => event.code === 'heartbeat_ack_timeout'));
  assert.equal(h.sockets.length, 2);
  hello(h.sockets[1]);
  assert.equal(h.sockets[1].sent[0].op, 6);
});

test('server opcode 1 receives an immediate heartbeat and ACK keeps it healthy', () => {
  const h = harness({ random: 0.5 });
  hello(h.sockets[0], 100);
  h.sockets[0].receive({ op: 1, d: null });
  assert.deepEqual(h.sockets[0].sent.at(-1), { op: 1, d: null });
  h.sockets[0].receive({ op: 11, d: null });
  h.clock.advance(50);
  assert.equal(h.sockets[0].sent.at(-1).op, 1);
  h.sockets[0].receive({ op: 11, d: null });
  h.clock.advance(100);
  assert.equal(h.sockets.length, 1);
  assert.equal(h.capture.health().heartbeat.timeouts, 0);
  assert.ok(h.diagnostics.some((event) => event.code === 'server_heartbeat_answered'));
});

test('READY must contain current-user identity instead of trusting config', () => {
  const h = harness();
  hello(h.sockets[0]);
  ready(h.sockets[0], { botId: null });
  assert.equal(h.capture.health().status, 'fatal');
  assert.equal(h.fatals[0].code, 'ready_identity_missing');
});

test('RESUMED without a pending Resume is a fatal protocol diagnosis', () => {
  const h = harness();
  hello(h.sockets[0]);
  ready(h.sockets[0]);
  h.sockets[0].receive({ op: 0, t: 'RESUMED', s: 2, d: {} });
  assert.equal(h.capture.health().status, 'fatal');
  assert.equal(h.fatals[0].code, 'unexpected_resumed');
});

test('malformed target-channel messages fail instead of losing native identity', () => {
  const h = harness();
  hello(h.sockets[0]);
  ready(h.sockets[0]);
  h.sockets[0].receive({
    op: 0,
    t: 'MESSAGE_CREATE',
    s: 2,
    d: {
      channel_id: 'table-channel',
      timestamp: '2026-08-01T00:00:00.000Z',
      author: { id: 'player-1', username: 'Rowan' },
      content: 'missing message ID',
      embeds: [],
    },
  });
  assert.equal(h.capture.health().status, 'fatal');
  assert.equal(h.fatals[0].code, 'invalid_message_create');
  assert.deepEqual(h.messages, []);
});
