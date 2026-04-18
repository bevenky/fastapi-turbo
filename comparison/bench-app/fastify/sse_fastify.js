// Fastify SSE bench — token-stream pattern matching vLLM/SGLang.
const Fastify = require('fastify');

const app = Fastify({ logger: false });

app.get('/stream', async (req, reply) => {
    const n = Math.max(1, Math.min(4096, parseInt(req.query.n || '32', 10)));
    reply.raw.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
    });
    for (let i = 0; i < n; i++) {
        reply.raw.write(`data: {"idx":${i},"delta":"tok"}\n\n`);
    }
    reply.raw.write('data: [DONE]\n\n');
    reply.raw.end();
    return reply;
});

app.get('/health', async () => 'ok');

const port = parseInt(process.env.PORT || '19502', 10);
app.listen({ host: '127.0.0.1', port });
