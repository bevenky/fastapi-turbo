const fastify = require('fastify')({ logger: false });

// WebSocket support
fastify.register(require('@fastify/websocket'));
fastify.register(async function (fastify) {
    fastify.get('/ws', { websocket: true }, (socket, req) => {
        socket.on('message', (message) => socket.send(message));
    });
});

fastify.get('/_ping', async () => ({ ping: 'pong' }));
fastify.get('/hello', async () => ({ message: 'hello' }));
fastify.get('/with-deps', async (req) => {
    const auth = req.headers.authorization || 'token';
    const db = { connected: true };
    const user = { name: 'alice' };
    return { user: user.name };
});
fastify.post('/items', async (req) => {
    const { name, price } = req.body;
    return { name, price, created: true };
});
fastify.post('/form-items', async (req) => {
    const { name, price } = req.body;
    return { name, price: parseFloat(price), created: true };
});

const port = process.env.PORT || 8004;
fastify.listen({ port, host: '127.0.0.1' }).then(() => {
    console.log(`Fastify running on http://127.0.0.1:${port}`);
});
