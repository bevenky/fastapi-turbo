// Node.js HTTP client parallel benchmark.
// Uses undici (same engine as Fastify) for max performance.
// Makes N parallel GET requests via Promise.all.

const { Pool } = require('undici');
// Fallback to http.request if undici is not available
let usingUndici = true;

async function main() {
    const [url, parallelStr, itersStr] = process.argv.slice(2);
    if (!url) {
        console.error('Usage: node bench_node.js <url> <parallel_count> <iterations>');
        process.exit(1);
    }
    const parallel = parseInt(parallelStr);
    const iters = parseInt(itersStr);
    const warmup = 500;

    const u = new URL(url);
    const origin = `${u.protocol}//${u.host}`;
    const path = u.pathname + u.search;

    // undici Pool — shared keep-alive connection pool
    const pool = new Pool(origin, {
        connections: parallel * 2,
        pipelining: 1,
        keepAliveTimeout: 30000,
    });

    async function doRequest() {
        const { body } = await pool.request({ path, method: 'GET' });
        let buf = '';
        for await (const chunk of body) {
            buf += chunk;
        }
        return buf;
    }

    async function doParallel(n) {
        const promises = [];
        for (let i = 0; i < n; i++) {
            promises.push(doRequest());
        }
        await Promise.all(promises);
    }

    // Warmup
    for (let i = 0; i < warmup; i++) {
        await doParallel(parallel);
    }

    // Benchmark
    const times = [];
    for (let i = 0; i < iters; i++) {
        const t0 = process.hrtime.bigint();
        await doParallel(parallel);
        const elapsed = Number(process.hrtime.bigint() - t0) / 1000; // microseconds
        times.push(elapsed);
    }

    times.sort((a, b) => a - b);
    const p50 = times[Math.floor(iters / 2)];
    const p99 = times[Math.floor(iters * 0.99)];
    const mn = times[0];

    console.log(JSON.stringify({
        parallel,
        iterations: iters,
        p50_us: Math.round(p50),
        p99_us: Math.round(p99),
        min_us: Math.round(mn),
        per_req_us: Math.round(p50 / parallel),
    }));

    await pool.close();
}

main().catch(err => { console.error(err); process.exit(1); });
