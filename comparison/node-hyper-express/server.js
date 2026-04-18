const HyperExpress = require('hyper-express');
const app = new HyperExpress.Server();

app.get('/_ping', (req, res) => {
    res.json({ ping: 'pong' });
});

app.get('/hello', (req, res) => {
    res.json({ message: 'hello' });
});

app.get('/with-deps', (req, res) => {
    const auth = req.header('authorization') || 'token';
    const db = { connected: true };
    const user = { name: 'alice' };
    res.json({ user: user.name });
});

app.post('/items', async (req, res) => {
    const body = await req.json();
    if (!body.name || body.price === undefined) {
        return res.status(422).json({ detail: 'validation error' });
    }
    res.json({ name: body.name, price: body.price, created: true });
});

app.post('/form-items', async (req, res) => {
    const body = await req.urlencoded();
    if (!body.name || body.price === undefined) {
        return res.status(422).json({ detail: 'validation error' });
    }
    res.json({ name: body.name, price: parseFloat(body.price), created: true });
});

const port = process.env.PORT || 8004;
app.listen(port, '127.0.0.1')
    .then(() => console.log(`Hyper-Express running on http://127.0.0.1:${port}`))
    .catch(err => {
        console.error('Hyper-Express failed, trying Fastify...');
        // Fallback: if hyper-express fails, signal to use fastify
        process.exit(1);
    });
