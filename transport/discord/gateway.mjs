// Dependency-free Discord Gateway state machine.
//
// This module deliberately owns connection health, not durable delivery. Its
// sequence/session state lives only in memory. A downstream commit handshake
// is required before table-kit can truthfully persist a capture checkpoint or
// claim cold-start completeness.

export const GATEWAY_URL = 'wss://gateway.discord.gg/?v=10&encoding=json';
export const GATEWAY_INTENTS = 512 | 32768; // GUILD_MESSAGES | MESSAGE_CONTENT
export const IDENTIFY_INTERVAL_MS = 5000;

export const APPLICATION_FLAGS = Object.freeze({
  GATEWAY_MESSAGE_CONTENT: 1 << 18,
  GATEWAY_MESSAGE_CONTENT_LIMITED: 1 << 19,
});

const FATAL_CLOSES = new Map([
  [4004, ['authentication_failed', 'the bot token was rejected']],
  [4010, ['invalid_shard', 'the configured shard is invalid']],
  [4011, ['sharding_required', 'Discord requires this application to shard']],
  [4012, ['invalid_api_version', 'the Gateway API version is invalid']],
  [4013, ['invalid_intents', 'the Identify payload contains invalid intents']],
  [4014, ['disallowed_intents', 'MESSAGE_CONTENT is not enabled or approved']],
]);

const IDENTIFY_CLOSES = new Map([
  [1000, ['normal_closure', 'the prior Gateway session cannot be resumed']],
  [1001, ['going_away', 'the prior Gateway session cannot be resumed']],
  [4007, ['invalid_sequence', 'the prior Gateway sequence cannot be resumed']],
  [4009, ['session_timed_out', 'the prior Gateway session timed out']],
]);

const RECOVERABLE_CLOSES = new Map([
  [4000, ['unknown_error', 'Discord permits reconnecting']],
  [4001, ['unknown_opcode', 'Discord permits reconnecting']],
  [4002, ['decode_error', 'Discord permits reconnecting']],
  [4003, ['not_authenticated', 'attempt to resume when session data exists']],
  [4005, ['already_authenticated', 'attempt to resume the existing session']],
  [4008, ['rate_limited', 'reconnect after backoff']],
]);

export function diagnoseClose(code, reason = '') {
  const normalized = Number.isInteger(code) ? code : 1006;
  let action = 'resume';
  let entry = RECOVERABLE_CLOSES.get(normalized);
  if (FATAL_CLOSES.has(normalized)) {
    action = 'stop';
    entry = FATAL_CLOSES.get(normalized);
  } else if (IDENTIFY_CLOSES.has(normalized)) {
    action = 'identify';
    entry = IDENTIFY_CLOSES.get(normalized);
  } else if (normalized >= 4000 && normalized <= 4999 && !entry) {
    action = 'stop';
    entry = ['unknown_gateway_close',
      'unknown application close code; refusing an unsafe reconnect loop'];
  } else if (!entry) {
    entry = ['abnormal_disconnect', 'attempt to resume when session data exists'];
  }
  return {
    code: normalized,
    name: entry[0],
    message: entry[1],
    reason: String(reason || ''),
    action,
    fatal: action === 'stop',
    recoverable: action !== 'stop',
  };
}

export function messageContentCapability(ready) {
  const flags = ready?.application?.flags;
  if (!Number.isSafeInteger(flags) || flags < 0) {
    return {
      status: 'unknown',
      available: false,
      evidence: 'READY.application.flags missing or invalid',
    };
  }
  const mask = (APPLICATION_FLAGS.GATEWAY_MESSAGE_CONTENT
    | APPLICATION_FLAGS.GATEWAY_MESSAGE_CONTENT_LIMITED);
  const available = Boolean(flags & mask);
  return {
    status: available ? 'available' : 'unavailable',
    available,
    evidence: 'READY.application.flags',
    application_flags: flags,
  };
}

function gatewayUrl(base) {
  const parsed = new URL(base);
  if (parsed.protocol !== 'wss:') throw new Error('Gateway URL must use wss:');
  parsed.searchParams.set('v', '10');
  parsed.searchParams.set('encoding', 'json');
  return parsed.toString();
}

const systemTimers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (id) => clearInterval(id),
};

export class DiscordGatewayCapture {
  constructor({
    token,
    channelId,
    websocketFactory = (url) => new WebSocket(url),
    timers = systemTimers,
    now = () => Date.now(),
    random = () => Math.random(),
    onMessage = () => {},
    onDiagnostic = () => {},
    onFatal = () => {},
  }) {
    if (!token) throw new TypeError('token is required');
    if (!channelId) throw new TypeError('channelId is required');
    if (typeof websocketFactory !== 'function') {
      throw new TypeError('websocketFactory must be a function');
    }
    this.token = token;
    this.channelId = String(channelId);
    this.websocketFactory = websocketFactory;
    this.timers = timers;
    this.now = now;
    this.random = random;
    this.onMessage = onMessage;
    this.onDiagnostic = onDiagnostic;
    this.onFatal = onFatal;

    this.socket = null;
    this.connectionMode = null;
    this.sessionId = null;
    this.resumeUrl = null;
    this.botUserId = null;
    this.lastIdentifyAt = null;
    this.backoffMs = 1000;
    this.reconnectTimer = null;
    this.firstHeartbeatTimer = null;
    this.heartbeatTimer = null;
    this.plannedReconnect = null;
    this.helloSeen = false;
    this.stopped = false;

    this.state = {
      status: 'idle',
      connection_mode: null,
      sequence: null,
      identity: { bot_user_id: null, source: null },
      capability: {
        message_content: 'unknown',
        evidence: null,
        requested_intents: GATEWAY_INTENTS,
        rest_fallback: false,
      },
      session: { resumable: false, resume_attempts: 0, successful_resumes: 0 },
      heartbeat: {
        interval_ms: null,
        awaiting_ack: false,
        last_sent_at: null,
        last_ack_at: null,
        sent: 0,
        timeouts: 0,
      },
      reconnect: {
        attempts: 0,
        backoff_ms: this.backoffMs,
        next_action: null,
        last_close: null,
      },
      identify: {
        attempts: 0,
        last_sent_at: null,
        minimum_interval_ms: IDENTIFY_INTERVAL_MS,
        session_start_limit: {
          status: 'not_queried',
          remaining: null,
          reset_after_ms: null,
          max_concurrency: null,
        },
      },
      coverage: {
        mode: 'live_uncheckpointed',
        durable_checkpoint: false,
        cold_start_backfill: false,
        gap_detection: 'not_implemented',
        known_gap_count: null,
        authoritative: false,
      },
      observation: {
        storage: 'memory_only',
        last_gateway_event_at: null,
        staleness_s: null,
        last_message_id: null,
        last_message_sequence: null,
        last_message_event_at: null,
      },
      last_event: null,
      last_error: null,
    };
  }

  health() {
    const snapshot = JSON.parse(JSON.stringify(this.state));
    if (snapshot.observation.last_gateway_event_at !== null) {
      snapshot.observation.staleness_s = Math.max(
        0, this.now() / 1000 - snapshot.observation.last_gateway_event_at);
    }
    return snapshot;
  }

  start() {
    if (this.state.status !== 'idle') throw new Error('Gateway capture already started');
    this._open('identify');
    return this;
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.state.status = 'stopped';
    this.state.session.resumable = false;
    this._clearReconnect();
    this._clearHeartbeat();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      try { socket.close(1000, 'operator stop'); } catch { /* already closed */ }
    }
    this._diagnostic('stopped', 'info');
  }

  _diagnostic(code, level, details = {}) {
    const event = {
      component: 'discord_gateway',
      code,
      level,
      ts: this.now() / 1000,
      ...details,
    };
    this.state.last_event = { code, level, ts: event.ts };
    this.onDiagnostic(event, this.health());
    return event;
  }

  _fatal(code, message, details = {}) {
    if (this.state.status === 'fatal') return;
    this._clearReconnect();
    this._clearHeartbeat();
    this.state.status = 'fatal';
    this.state.session.resumable = false;
    this.state.last_error = { code, message, ...details };
    const diagnostic = this._diagnostic(code, 'fatal', { message, ...details });
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      try { socket.close(1000, code); } catch { /* already closed */ }
    }
    this.onFatal(diagnostic, this.health());
  }

  _open(mode) {
    if (this.stopped || this.state.status === 'fatal') return;
    this._clearReconnect();
    this._clearHeartbeat();
    this.connectionMode = mode === 'resume' && this._canResume() ? 'resume' : 'identify';
    this.state.connection_mode = this.connectionMode;
    this.state.status = 'connecting';
    this.state.reconnect.next_action = null;
    this.helloSeen = false;

    let url = GATEWAY_URL;
    try {
      if (this.connectionMode === 'resume') url = gatewayUrl(this.resumeUrl);
      const socket = this.websocketFactory(url);
      this.socket = socket;
      socket.onmessage = (event) => {
        if (this.socket !== socket) return;
        try {
          this._receive(event.data);
        } catch (error) {
          this._fatal('gateway_handler_failed', 'Gateway event handling failed', {
            detail: String(error?.message || error),
          });
        }
      };
      socket.onclose = (event) => {
        if (this.socket !== socket) return;
        this.socket = null;
        this._closed(event);
      };
      socket.onerror = () => {
        if (this.socket === socket) this._diagnostic('socket_error', 'error');
      };
      this._diagnostic('connecting', 'info', {
        mode: this.connectionMode,
        resume_url: this.connectionMode === 'resume',
      });
    } catch (error) {
      this.socket = null;
      this._scheduleReconnect(this.connectionMode, {
        code: 'socket_open_failed',
        message: String(error?.message || error),
      });
    }
  }

  _receive(raw) {
    if (this.stopped || this.state.status === 'fatal') return;
    let payload = raw;
    try {
      if (typeof raw === 'string') payload = JSON.parse(raw);
    } catch (error) {
      this._fatal('invalid_gateway_payload', 'Gateway sent malformed JSON', {
        detail: String(error.message),
      });
      return;
    }
    if (!payload || typeof payload !== 'object' || !Number.isInteger(payload.op)) {
      this._fatal('invalid_gateway_payload', 'Gateway payload has no integer opcode');
      return;
    }
    this.state.observation.last_gateway_event_at = this.now() / 1000;
    if (payload.op === 0) {
      if (!Number.isSafeInteger(payload.s) || payload.s < 0) {
        this._fatal('invalid_gateway_sequence', 'Dispatch has no valid sequence number');
        return;
      }
      this.state.sequence = payload.s;
    }

    if (payload.op === 10) this._hello(payload.d);
    else if (payload.op === 11) this._heartbeatAck();
    else if (payload.op === 1) this._sendHeartbeat('server_request');
    else if (payload.op === 7) {
      this._requestReconnect('resume', 'server_reconnect', 0);
    } else if (payload.op === 9) {
      this._invalidSession(Boolean(payload.d));
    } else if (payload.op === 0 && payload.t === 'READY') {
      this._ready(payload.d);
    } else if (payload.op === 0 && payload.t === 'RESUMED') {
      this._resumed();
    } else if (payload.op === 0 && payload.t === 'MESSAGE_CREATE') {
      this._message(payload.d);
    }
  }

  _hello(data) {
    if (this.helloSeen) {
      this._fatal('duplicate_hello', 'Gateway sent Hello more than once on one connection');
      return;
    }
    if (this.state.status !== 'connecting') {
      this._fatal('unexpected_hello', 'Hello arrived outside connection setup', {
        status: this.state.status,
      });
      return;
    }
    const interval = data?.heartbeat_interval;
    if (!Number.isFinite(interval) || interval <= 0) {
      this._fatal('invalid_hello', 'Hello has no positive heartbeat interval');
      return;
    }
    this.helloSeen = true;
    this.state.heartbeat.interval_ms = interval;
    const jitter = Math.max(0, Math.min(0.999999, Number(this.random()) || 0));
    const firstDelay = Math.floor(interval * jitter);
    const socket = this.socket;
    this.firstHeartbeatTimer = this.timers.setTimeout(() => {
      this.firstHeartbeatTimer = null;
      if (this.socket !== socket) return;
      this._sendHeartbeat('interval');
      if (this.socket !== socket || !['identifying', 'resuming', 'ready']
        .includes(this.state.status)) return;
      this.heartbeatTimer = this.timers.setInterval(
        () => this._sendHeartbeat('interval'), interval);
    }, firstDelay);

    if (this.connectionMode === 'resume' && this._canResume()) {
      this.state.status = 'resuming';
      const sent = this._send({
        op: 6,
        d: { token: this.token, session_id: this.sessionId, seq: this.state.sequence },
      });
      if (sent) {
        this.state.session.resume_attempts += 1;
        this._diagnostic('resume_sent', 'info', { sequence: this.state.sequence });
      }
    } else {
      this.state.status = 'identifying';
      const sent = this._send({
        op: 2,
        d: {
          token: this.token,
          intents: GATEWAY_INTENTS,
          properties: {
            os: process.platform,
            browser: 'table-kit',
            device: 'table-kit',
          },
        },
      });
      if (sent) {
        this.lastIdentifyAt = this.now();
        this.state.identify.attempts += 1;
        this.state.identify.last_sent_at = this.lastIdentifyAt / 1000;
        this._diagnostic('identify_sent', 'info', {
          attempt: this.state.identify.attempts,
          session_start_limit: this.state.identify.session_start_limit,
        });
      }
    }
  }

  _ready(data) {
    if (this.state.status !== 'identifying') {
      this._fatal('unexpected_ready', 'READY arrived when no Identify was pending', {
        status: this.state.status,
      });
      return;
    }
    const botId = data?.user?.id;
    const sessionId = data?.session_id;
    const resumeUrl = data?.resume_gateway_url;
    if (!botId || typeof botId !== 'string') {
      this._fatal('ready_identity_missing', 'READY did not contain the current bot user ID');
      return;
    }
    if (!sessionId || typeof sessionId !== 'string') {
      this._fatal('ready_session_missing', 'READY did not contain a session ID');
      return;
    }
    try {
      gatewayUrl(resumeUrl);
    } catch {
      this._fatal('ready_resume_url_invalid', 'READY did not contain a valid resume URL');
      return;
    }
    const capability = messageContentCapability(data);
    this.state.capability = {
      message_content: capability.status,
      evidence: capability.evidence,
      requested_intents: GATEWAY_INTENTS,
      rest_fallback: false,
    };
    if (!capability.available) {
      this._fatal(
        capability.status === 'unknown'
          ? 'message_content_capability_unknown'
          : 'message_content_unavailable',
        'Cannot claim complete table capture without MESSAGE_CONTENT capability',
        { evidence: capability.evidence });
      return;
    }

    this.botUserId = botId;
    this.sessionId = sessionId;
    this.resumeUrl = resumeUrl;
    this.state.identity = { bot_user_id: botId, source: 'READY.user.id' };
    this.state.session.resumable = true;
    this.state.status = 'ready';
    this.backoffMs = 1000;
    this.state.reconnect.backoff_ms = this.backoffMs;
    this._diagnostic('ready', 'info', {
      bot_user_id: botId,
      username: String(data.user.username || ''),
      message_content: capability.status,
      coverage: this.state.coverage.mode,
    });
  }

  _resumed() {
    if (this.state.status !== 'resuming' || !this._canResume() || !this.botUserId) {
      this._fatal('unexpected_resumed', 'RESUMED arrived without READY session state');
      return;
    }
    this.state.status = 'ready';
    this.state.session.successful_resumes += 1;
    this.backoffMs = 1000;
    this.state.reconnect.backoff_ms = this.backoffMs;
    this._diagnostic('resumed', 'info', { sequence: this.state.sequence });
  }

  _message(message) {
    if (!this.botUserId || this.state.capability.message_content !== 'available') {
      this._fatal('message_before_capability',
        'Message arrived before READY established identity and content capability');
      return;
    }
    if (!message || String(message.channel_id) !== this.channelId) return;
    const parsedTime = Date.parse(message.timestamp);
    if (typeof message.id !== 'string' || !message.id
      || typeof message.author?.id !== 'string' || !message.author.id
      || !Number.isFinite(parsedTime)
      || typeof message.content !== 'string'
      || !Array.isArray(message.embeds)) {
      this._fatal('invalid_message_create',
        'Target-channel MESSAGE_CREATE is missing required native fields');
      return;
    }
    if (String(message.author?.id || '') === this.botUserId) return;
    this.state.observation.last_message_id = message.id;
    this.state.observation.last_message_sequence = this.state.sequence;
    this.state.observation.last_message_event_at = parsedTime / 1000;
    this.onMessage({
      ts: Number.isFinite(parsedTime) ? parsedTime / 1000 : null,
      id: message.id,
      author: message.author?.username || 'unknown',
      author_id: message.author?.id,
      is_bot: Boolean(message.author?.bot),
      content: message.content,
      embeds: message.embeds,
    });
  }

  _send(payload) {
    try {
      this.socket.send(JSON.stringify(payload));
      return true;
    } catch (error) {
      this._requestReconnect('resume', 'gateway_send_failed', 0, {
        detail: String(error?.message || error),
      });
      return false;
    }
  }

  _sendHeartbeat(source) {
    if (!this.socket || this.state.status === 'fatal') return;
    if (source === 'interval' && this.state.heartbeat.awaiting_ack) {
      this.state.heartbeat.timeouts += 1;
      this._diagnostic('heartbeat_ack_timeout', 'error', {
        sequence: this.state.sequence,
      });
      this._requestReconnect('resume', 'heartbeat_ack_timeout', 0);
      return;
    }
    if (!this._send({ op: 1, d: this.state.sequence })) return;
    this.state.heartbeat.awaiting_ack = true;
    this.state.heartbeat.last_sent_at = this.now() / 1000;
    this.state.heartbeat.sent += 1;
    if (source === 'server_request') {
      this._diagnostic('server_heartbeat_answered', 'info', {
        sequence: this.state.sequence,
      });
    }
  }

  _heartbeatAck() {
    this.state.heartbeat.awaiting_ack = false;
    this.state.heartbeat.last_ack_at = this.now() / 1000;
  }

  _invalidSession(canResume) {
    if (canResume && this._canResume()) {
      this._requestReconnect('resume', 'invalid_session_resumable', 0);
      return;
    }
    this._invalidateSession();
    const delay = 1000 + Math.floor(Math.max(0, Math.min(0.999999,
      Number(this.random()) || 0)) * 4000);
    this._requestReconnect('identify', 'invalid_session_fresh_identify', delay);
  }

  _requestReconnect(action, code, delay = 0, details = {}) {
    if (this.stopped || this.state.status === 'fatal' || this.plannedReconnect) return;
    if (action === 'identify') this._invalidateSession();
    this.plannedReconnect = { action, code, delay, details };
    this.state.status = 'closing';
    this._clearHeartbeat();
    const socket = this.socket;
    if (!socket) {
      this.plannedReconnect = null;
      this._scheduleReconnect(action, { code, ...details }, delay);
      return;
    }
    try {
      socket.close(4000, code);
    } catch {
      this.socket = null;
      this.plannedReconnect = null;
      this._scheduleReconnect(action, { code, ...details }, delay);
    }
  }

  _closed(event = {}) {
    this._clearHeartbeat();
    if (this.stopped || this.state.status === 'fatal') return;
    const close = diagnoseClose(event.code, event.reason);
    this.state.reconnect.last_close = close;
    if (close.fatal) {
      this._fatal(close.name, close.message, {
        close_code: close.code,
        close_reason: close.reason,
        action: close.action,
      });
      return;
    }
    const planned = this.plannedReconnect;
    this.plannedReconnect = null;
    let action = close.action === 'identify'
      ? 'identify'
      : (planned?.action || close.action);
    if (action === 'resume' && !this._canResume()) action = 'identify';
    if (action === 'identify') this._invalidateSession();
    const requestedDelay = planned ? planned.delay : this.backoffMs;
    const delay = this._conformReconnectDelay(action, requestedDelay);
    this._diagnostic('gateway_closed', 'error', {
      close_code: close.code,
      close_name: close.name,
      close_reason: close.reason,
      action,
      retry_in_ms: delay,
      trigger: planned?.code || close.name,
    });
    this._scheduleReconnect(action, planned || close, delay);
  }

  _scheduleReconnect(action, diagnosis, delay = this.backoffMs) {
    if (this.stopped || this.state.status === 'fatal') return;
    this._clearReconnect();
    const retryDelay = this._conformReconnectDelay(action, delay);
    this.state.status = 'backoff';
    this.state.reconnect.attempts += 1;
    this.state.reconnect.next_action = action;
    this.state.reconnect.backoff_ms = retryDelay;
    this._diagnostic('reconnect_scheduled', 'info', {
      action,
      retry_in_ms: retryDelay,
      cause: diagnosis.code || diagnosis.name,
    });
    this.reconnectTimer = this.timers.setTimeout(() => {
      this.reconnectTimer = null;
      this._open(action);
    }, retryDelay);
    this.backoffMs = Math.min(Math.max(this.backoffMs * 2, 1000), 30000);
  }

  _conformReconnectDelay(action, delay) {
    if (action !== 'identify' || this.lastIdentifyAt === null) return delay;
    return Math.max(delay,
      IDENTIFY_INTERVAL_MS - Math.max(0, this.now() - this.lastIdentifyAt));
  }

  _canResume() {
    return Boolean(this.sessionId && this.resumeUrl
      && Number.isSafeInteger(this.state.sequence));
  }

  _invalidateSession() {
    this.sessionId = null;
    this.resumeUrl = null;
    this.botUserId = null;
    this.state.sequence = null;
    this.state.identity = { bot_user_id: null, source: null };
    this.state.capability = {
      message_content: 'unknown',
      evidence: null,
      requested_intents: GATEWAY_INTENTS,
      rest_fallback: false,
    };
    this.state.session.resumable = false;
  }

  _clearReconnect() {
    if (this.reconnectTimer !== null) {
      this.timers.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  _clearHeartbeat() {
    if (this.firstHeartbeatTimer !== null) {
      this.timers.clearTimeout(this.firstHeartbeatTimer);
      this.firstHeartbeatTimer = null;
    }
    if (this.heartbeatTimer !== null) {
      this.timers.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    this.state.heartbeat.awaiting_ack = false;
    this.state.heartbeat.interval_ms = null;
  }
}
