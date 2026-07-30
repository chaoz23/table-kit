// table-kit resident server: boardgame.io Server + FlatFile persistence.
// Loopback only. State survives restarts; the log replays from the seed.
const { Server, FlatFile, Origins } = require('boardgame.io/server');
const path = require('path');
const { AgentTable } = require('./game.cjs');

const server = Server({
  games: [AgentTable],
  db: new FlatFile({ dir: path.join(__dirname, 'data', 'matches'), logging: false }),
  origins: [Origins.LOCALHOST],
});

const PORT = 18800;
server.run(PORT, () => console.log(`table-kit server on 127.0.0.1:${PORT}`));
