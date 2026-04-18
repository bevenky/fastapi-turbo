// File handling benchmark server — Node Fastify.
//
// Endpoints:
//   POST /upload          — multipart/form-data, single file
//   GET  /download/:name  — reply.sendFile
//   GET  /static/:name    — @fastify/static mount
//   GET  /health

const fs = require('fs');
const os = require('os');
const path = require('path');

const Fastify = require('fastify');
const fastifyMultipart = require('@fastify/multipart');
const fastifyStatic = require('@fastify/static');
const fastifyCompress = require('@fastify/compress');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bench_files_js_'));

const files = {
    'small.txt':  Buffer.from('hello world\n'.repeat(10)),
    'medium.bin': Buffer.alloc(64 * 1024, 'x'),
    'large.bin':  Buffer.alloc(1024 * 1024, 'x'),
    'style.css':  Buffer.from('body{color:red;}'.repeat(100)),
};
for (const [name, data] of Object.entries(files)) {
    fs.writeFileSync(path.join(tmpDir, name), data);
}

// Compressible JSON payload (~60 KB)
const bigItems = Array.from({ length: 200 }, (_, i) => ({
    id: i,
    name: `item-${i}`,
    desc: 'lorem ipsum dolor sit amet '.repeat(4),
}));

const mime = { '.txt': 'text/plain', '.bin': 'application/octet-stream', '.css': 'text/css' };

async function main() {
    const app = Fastify({ logger: false });
    // Compression plugin — MUST be awaited so it's active when routes register.
    await app.register(fastifyCompress, {
        global: true,
        threshold: 500,
        encodings: ['gzip', 'deflate'],
    });
    await app.register(fastifyMultipart);
    await app.register(fastifyStatic, { root: tmpDir, prefix: '/static/' });

    app.get('/health', async () => ({ ok: true }));
    app.get('/json-big', async () => bigItems);

    app.post('/upload', async (req, reply) => {
        const parts = req.parts();
        for await (const part of parts) {
            if (part.file) {
                const chunks = [];
                for await (const chunk of part.file) {
                    chunks.push(chunk);
                }
                const data = Buffer.concat(chunks);
                return { filename: part.filename, size: data.length };
            }
        }
        reply.code(400);
        return { err: 'no file' };
    });

    // Manual file stream — avoids @fastify/static decorateReply scoping issues
    app.get('/download/:name', (req, reply) => {
        const name = req.params.name;
        const file = path.join(tmpDir, name);
        const ext = path.extname(name).toLowerCase();
        reply.type(mime[ext] || 'application/octet-stream');
        const st = fs.statSync(file);
        reply.header('content-length', st.size);
        return reply.send(fs.createReadStream(file));
    });

    const port = parseInt(process.env.PORT || '8300');
    await app.listen({ host: '127.0.0.1', port });
}

main().catch(err => { console.error(err); process.exit(1); });
