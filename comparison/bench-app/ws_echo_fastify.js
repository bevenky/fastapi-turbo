// Fastify WebSocket echo server — text and binary.
const Fastify = require('fastify');
const websocketPlugin = require('@fastify/websocket');

const fastify = Fastify({ logger: false });
fastify.register(websocketPlugin);

fastify.register(async (app) => {
    app.get('/ws-text', { websocket: true }, (conn) => {
        // Fastify websocket v11+: conn is the WebSocket directly
        conn.on('message', (msg) => {
            // msg is Buffer; send as text
            conn.send(msg.toString());
        });
    });

    app.get('/ws-bytes', { websocket: true }, (conn) => {
        conn.on('message', (msg) => {
            conn.send(msg);  // Buffer → binary frame
        });
    });
});

const port = parseInt(process.env.PORT || '8820');
fastify.listen({ host: '127.0.0.1', port }, (err) => {
    if (err) {
        console.error(err);
        process.exit(1);
    }
});
