// Fastify comparison server with onRequest hook + error handler
// to match fastapi-turbo feature set.

const Fastify = require('fastify');
const fastify = Fastify({ logger: false });

// Middleware equivalent: onRequest + onSend hooks
fastify.addHook('onSend', async (request, reply, payload) => {
    reply.header('x-mw', 'y');
    return payload;
});

// Custom error handler (@app.exception_handler equivalent)
fastify.setErrorHandler(async (error, request, reply) => {
    reply.status(error.statusCode || 500).send({ err: error.message });
});

fastify.get('/hello', async (req, reply) => {
    return { message: 'hello' };
});

fastify.get('/hello-html', async (req, reply) => {
    reply.type('text/html');
    return '<h1>hi</h1>';
});

fastify.get('/hello-cookie', async (req, reply) => {
    reply.header('set-cookie', 'session=abc123; Path=/; SameSite=Lax');
    return { message: 'hello' };
});

fastify.get('/err', async (req, reply) => {
    const err = new Error('test error');
    err.statusCode = 400;
    throw err;
});

const port = parseInt(process.env.PORT || '8300');
fastify.listen({ host: '127.0.0.1', port }, (err) => {
    if (err) { console.error(err); process.exit(1); }
});
