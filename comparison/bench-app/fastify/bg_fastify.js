// Fastify background task equivalent: fire-and-forget via setImmediate.
const crypto = require('crypto');
const Fastify = require('fastify');

const app = Fastify({ logger: false });

let count = 0;

function noop() {
    count++;
}

function writeLog(msg) {
    count++;
}

function cpuWork() {
    const h = crypto.createHash('sha256');
    for (let i = 0; i < 1000; i++) h.update('x');
    h.digest();
    count++;
}

app.post('/bg/sync', async () => {
    setImmediate(noop);
    return { ok: true };
});

app.post('/bg/write', async () => {
    setImmediate(() => writeLog('m'));
    return { ok: true };
});

app.post('/bg/cpu', async () => {
    setImmediate(cpuWork);
    return { ok: true };
});

app.get('/bg/count', async () => ({ count }));
app.get('/health', async () => ({ ok: true }));

const port = parseInt(process.env.PORT || '8920');
app.listen({ host: '127.0.0.1', port });
