import Client, { timeboxPromise, AsyncQueue } from '../client';

function timeoutPromise(resolveValue, time) {
    return new Promise(resolve => setTimeout(() => resolve(resolveValue), time));
}

async function sleep(time) {
    return timeoutPromise(true, time);
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