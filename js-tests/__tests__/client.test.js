import Client, { timeboxPromise, AsyncQueue, Convo } from '../client';
import WebSocket from 'ws'

function timeoutPromise(resolveValue, time) {
    return new Promise(resolve => setTimeout(() => resolve(resolveValue), time));
}

async function sleep(time) {
    return timeoutPromise(true, time);
}

class MockClient {
    constructor() {
        this.sends = [];
        this.gets = [];
        this._getMessageAsync = async guid => {
            return this.gets.shift();
        };
        this.getMessageAsync = this._getMessageAsync;

        this._send = data => this.sends.push(data);
        this.send = this._send;
    }

    reset() {
        this.sends = [];
        this.gets = [];
        this.getMessageAsync = this._getMessageAsync;
        this.send = this._send;
    }
}

describe('timeboxPromise', () => {
    it('times out', async () => {
        await expect(timeboxPromise(timeoutPromise('yo', 200), 180)).rejects.toThrow(/^Promise did not resolve/);
    });

    it('resolves in time', async () => {
        await expect(timeboxPromise(timeoutPromise('yo', 180), 200)).resolves.toBe('yo');
    });
});

describe('AsyncQueue', () => {
    it('gets one item', async () => {
        const queue = new AsyncQueue();

        queue.put('foo');

        expect(await queue.getAsync()).toBe('foo');
    });

    it('gets several items', async () => {
        const queue = new AsyncQueue();

        const items = [ 'foo', 'foo2', 'foo3' ];

        items.forEach(item => queue.put(item));
        items.forEach(async item => expect(await queue.getAsync()).toBe(item));
    });

    it('gets one item after a 50ms', async () => {
        const queue = new AsyncQueue();

        setTimeout(() => queue.put('hello!'), 50);

        expect(await queue.getAsync()).toBe('hello!');
    });

    it('gets a bunch of items and gets one more later after 50ms', async () => {
        const queue = new AsyncQueue();

        const items = [ 'foo', 'foo2', 'foo3' ];

        items.forEach(item => queue.put(item));
        items.forEach(async item => expect(await queue.getAsync()).toBe(item));


        setTimeout(() => queue.put('hello!'), 50);
        expect(await queue.getAsync()).toBe('hello!');
    });

    it('gets a few items 10ms each', async () => {
        const queue = new AsyncQueue();

        const items = ['foo', 'foo2', 'foo3'];

        (async () => {
            for (let i = 0, item; i < items.length; ++i) {
                item = items[i];
                await sleep(10);
                queue.put(item);
            }
        })();

        for (let i = 0, item; i < items.length; ++i) {
            item = items[i];
            expect(await queue.getAsync()).toBe(item);
        }
    });

    it('starve queue for 50ms', async () => {
        const queue = new AsyncQueue();

        const items = ['foo', 'foo2', 'foo3'];

        queue.put(items[0]);
        queue.put(items[1]);

        (async () => {
            await sleep(50);
            queue.put(items[2]);
        })();

        expect(await queue.getAsync()).toBe(items[0]);
        expect(await queue.getAsync()).toBe(items[1]);

        await expect(timeboxPromise((async () => {
            expect(await queue.getAsync()).toBe(items[2]);
        })(), 45)).rejects.toThrow(/^Promise did not resolve/);
    });
});

describe('Convo', () => {
    const uuid = '123F00';

    const client = new MockClient();

    beforeEach(() => client.reset());

    it('sends and expects a message successfully', async () => {
        const convo = new Convo(client, 'test_action', uuid);

        client.gets.push('hello');

        await (async convo => {
             expect(await convo.sendAndExpect({msg: 'yo'})).toBe('hello');
        })(convo);

        expect(client.sends).toContainEqual(
            expect.objectContaining({
                action: 'test_action',
                msg: 'yo',
                response_id: uuid,
            })
        );
    });

    it('times out on an expect', async () => {
        const convo = new Convo(client, 'test_action', uuid);

        client.gets.push('not suppose to get this');

        client.getMessageAsync = async guid => {
            await sleep(70);
            return null;
        };

        await expect(convo.sendAndExpect({msg: 'hey'}, 50)).rejects.toThrow(/^Promise did not resolve/);
    });
});

describe('Client', () => {

    class MockSocket {
        constructor(url) {
            this.sends = [];

            const notImpl = () => { throw new Error('Not implemented'); };

            this.onmessage = notImpl;
            this.onopen = notImpl;
            this.onclose = notImpl;
            this.onerror = notImpl;
            this.readyState = WebSocket.CONNECTING;
        }

        send(data) {
            this.sends.push(data);
        }

        addEventListener(event, fn) {
            this[`on${event}`] = fn;
        }

        mockServerSend(data) {
            this.onmessage({data});
        }

        mockConnect() {
            this.readyState = WebSocket.OPEN;
            this.onopen({});
        }

        getAllDecodedSends() {
            const results = [];
            this.sends.forEach(msg => results.push(JSON.parse(msg)));

            return results;
        }

        popDecoded() {
            if (this.sends.length === 0) {
                return null;
            }

            const top = this.sends.pop();
            return JSON.parse(top);
        }
    }

    it('delays connection for 10ms and sends message', async () => {
        const mockSocket = new MockSocket();
        const client = new Client('whatever', () => mockSocket);

        const promise = timeboxPromise(
            (async () => {
                const data = {data: 'yo'};

                await client.send(data);
                expect(mockSocket.getAllDecodedSends()).toEqual([data]);
            })(),
            20
        );

        await sleep(10);

        mockSocket.mockConnect();

        await promise;
    });

    it('has a conversation', async () => {
        const mockSocket = new MockSocket();
        const client = new Client('whatever', () => mockSocket);

        await client.convo('foo', async (convo, guid) => {
            let promise;
            let data = {data: 'hey'};

            mockSocket.mockConnect();

            promise = convo.sendAndExpect(data, 20);

            // Wait a while to so we get the hey
            await sleep(5);

            expect(mockSocket.popDecoded()).toEqual(expect.objectContaining(data));

            debugger;

            data = {hello: 'hi', response_id: guid};
            mockSocket.mockServerSend(JSON.stringify(data));

            expect(await promise).toEqual(expect.objectContaining(data));
        });
    });
});