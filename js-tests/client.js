import WebSocket from 'ws';
import _ from 'lodash';
import EventEmitter from 'events';
import MonotonicNow from 'monotonic-timestamp';
import uuidv4 from 'uuid/v4';

export default class Client extends EventEmitter {
    constructor(url, socketCreator = null) {
        super();

        this.ws = null;

        let _url;

        if (_.isString(url)) {
            _url = url;
        }
        else if (_.isFunction(url)) {
            _url = url();
        }
        else {
            throw new Error('Url parameter must be string or function');
        }

        this.ws = _.isFunction(socketCreator) ? socketCreator(_url) : new WebSocket(_url);

        this.ws.addEventListener('message', this.handleMessage.bind(this));
        this.ws.addEventListener('open', this.handleOpen.bind(this));
        this.ws.addEventListener('close', this.handleClose.bind(this));
        this.ws.addEventListener('error', this.handleError.bind(this));
        this.convos = {};
        this.queues = {};
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
        return _.get(obj, 'response_id', null) || _.get(obj, 'error_data.response_id', null);
    }

    _parseMessage(data) {
        try {
            const obj = JSON.parse(data);
            const guid = Client._extract_guid(obj);

            const convo = this.convos[guid] || null;

            let queue = this.queues[guid];

            if (!queue) {
                this.queues[guid] = queue = new AsyncQueue();
            }

            queue.put(obj);
        }
        catch (e) {
            console.error('Error parsing message:', e);
        }
    }

    async getMessageAsync(guid) {
        let queue = this.queues[guid];

        if (!queue) {
            this.queues[guid] = queue = new AsyncQueue();
        }

        return queue.getAsync();
    }

    async send(data, timeout=2000) {
        const json = JSON.stringify(data);
        const state = this.ws.readyState;

        switch (state) {
            case WebSocket.CONNECTING:
                console.log('Socket still opening, waiting to send', data);

                await this.wait('open', timeout).catch(data => { throw new Error(data); });
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

    async convo(actionName, asyncAction) {
        const guid = uuidv4();
        const convo = new Convo(this, actionName, guid);

        this.convos[convo] = convo;

        const maybePromise = asyncAction(convo, guid);

        if (!maybePromise instanceof Promise) {
            throw new Error('asyncAction must be promise/async');
        }

        try {
            const result = await maybePromise;
        }
        catch (e) {
            console.error(`Error occurred while in convo for ${actionName}:${guid}: ${e}`)
            throw e;
        }
        finally {
            delete this.convos[convo];

            if (guid in this.queues) {
                const queue = this.queues[guid];
                queue.close();
                delete this.queues[guid];
            }
        }
    }

    async close(code=1000, reason='') {
        return this.ws.close(code, reason);
    }

    wait(event, timeout) {
        let onEvent = null;

        return timeboxPromise(new Promise((resolve, reject) => {
                onEvent = data => resolve(data);
                this.once(event, onEvent);
            }),
            timeout,
            () => {
                if (onEvent) {
                    this.off(event, onEvent)
                }
            }
        );
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

    close() {
        if (this._reject) {
            this._reject(new Error('AsyncQueue closing'));
            this._reject = null;
            this._resolve = null;
        }
    }

    async getAsync() {
        if (this._resolve !== null) {
            throw new Error('AsyncQueue.get() already blocking');
        }

        if (this.items.length > 0) {
            return this.items.shift();
        }

        return new Promise((resolve, reject) => {
            this._resolve = resolve;
            this._reject = reject;
        });
    }

    get length() {
        return this.items.length;
    }
}

export class Convo {
    constructor(client, action, guid) {
        this.client         = client;
        this.guid           = guid;
        this.action         = action;
        this.startTimestamp = MonotonicNow();
    }

    async expect(timeout=5000.0) {
        return timeboxPromise(this.client.getMessageAsync(this.guid), timeout);
    }

    async send(data) {
        return this.client.send({
            ...data,
            action: this.action,
            response_id: this.guid,
        });
    }

    async sendAndExpect(data, timeout=5000.0) {
        await this.send(data);
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

            reject(new Error('Promise did not resolve in time'));
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