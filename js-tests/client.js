import WebSocket from 'ws';
import _ from 'lodash';
import EventEmitter from 'events';

export default class Client extends EventEmitter {
    constructor(url) {
        super();

        this.ws = new WebSocket(url);

        this.ws.addEventListener('message', this.handleMessage.bind(this));
        this.ws.addEventListener('open', this.handleOpen.bind(this));
        this.ws.addEventListener('close', this.handleClose.bind(this));
        this.ws.addEventListener('error', this.handleError.bind(this));
        this.convos = {};
    }

    handleOpen(data) {
        console.info('Connection socket opened, state:', this.ws.readyState);
        this.emit('open', data);
    }

    handleError(data) {
        console.error('Connection socket error occurred');
        this.emit('error', data);
    }

    handleClose(data) {
        // console.info('Connection Socket closed', data);
        this.emit('close', data);
    }

    handleMessage(data) {
        // this.emit('message', data);
        this._parseMessage(data.data);
    }

    static _extract_guid(obj) {
        return _.get(obj, 'response_id', null) || _.get(obj, 'error_data.response_id');
    }

    _parseMessage(data) {
        try {
            const obj = JSON.parse(data);
            const guid = Client._extract_guid(obj);

            const convo = this.convos[guid] || null;

            let queue = this.queues[guid];

            if (!queue) {
                this.queues[guid] = queue = [];
            }

            if (!convo) {
                queue.push(obj);
            }
            else {
                if (!convo.submitMessage(obj)) {
                    queue.push(obj);
                }
            }
        }
        catch (e) {
            console.error('Error parsing message:', e);
        }
    }

    getMessage(guid) {
        const queue = this.queues[guid];

        if (!queue || queue.length === 0) {
            return null;
        }

        return queue.shift();
    }

    async send(data, timeout=2000) {
        const json = JSON.stringify(data);
        const state = this.ws.readyState;

        switch (state) {
            case WebSocket.CONNECTING:
                console.log('Socket still opening, waiting to send', data);

                await this.waitForAsync('open', true, timeout, true,
                    data => new Error(data)
                );
                break;

            case WebSocket.OPEN:
                break;

            case WebSocket.CLOSED:
            case WebSocket.CLOSING:
                throw new Error('Socket is closing or closed!');

            default:
                throw Error('Unknown state ' + JSON.stringify(state));
        }

        console.log('Sending', data);

        return this.ws.send(json);
    }

    waitForReply(filter, timeout=2000) {
        return this.waitForAsync(
            'message',
            filter,
            timeout,
            true,
            () => new Error('Timed out waiting for reply'),
        )
    }

    close() {
        return this.ws.close();
    }
}

export class AsyncQueue {
    constructor() {
        this._resolve = null;
        this.items = [];
    }

    put(data) {
        if (this._resolve) {
            this._resolve(data);
            this._resolve = null;
            this._reject = null;
        } else {
            this.items.push(data);
        }
    }

    async getAsync() {
        if (this._resolve !== null) {
            throw new Error('AsyncQueue.get() already blocking');
        }

        if (this.items.length > 0) {
            return this.items.shift();
        }

        return new Promise((resolve, reject) => { this._resolve = resolve; } );
    }

    get length() {
        return this.items.length;
    }
}

class Convo {
    constructor(client, guid) {
        this.client = client;
        this.guid   = guid;
        this.queue = AsyncQueue();
    }

    async expect(timeout=5.0) {
    }

    async sendAndExpect(data, timeout=5.0) {
        await this.client.send({...data, response_id: this.guid}, timeout);
        return this.expect(timeout);
    }
}

export function timeboxPromise(promise, timeout, onTimeout=null) {
    if (!promise instanceof Promise) {
        throw new Error('Argument must be a promise');
    }

    return new Promise((resolve, reject) => {
        const timeoutCallback = () => {
            if (_.isFunction(onTimeout)) {
                onTimeout();
            }

            reject(Error('Promise did not resolve in time'));
        };

        const cancelTimeout = () => {
            clearTimeout(timeoutId);
        };

        const catchCallback = data => {
            clearTimeout();
            reject(data);
        };

        const timeoutId = setTimeout(timeoutCallback, timeout);

        promise.then(data => {
            cancelTimeout();
            resolve(promise);
        }).catch(catchCallback);
    });
}