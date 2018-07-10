import WebSocket from 'ws';
import _ from 'lodash';
import EventEmitter from 'events';
import MonotonicNow from 'monotonic-timestamp';
import { uuidv4 } from 'uuid/v4';

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

    async getMessageAsync(guid) {
        const queue = this.queues[guid];

        if (!queue) {
            this.queues[guid] = new AsyncQueue();
        }

        return this.queues.getAsync();
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

    async convo(actionName, asyncAction) {
        const guid = uuidv4();
        const convo = new Convo(this, actionName, guid);

        this.convos[convo] = convo;

        if (!asyncAction instanceof Promise) {
            throw new Error('asyncAction must be promise/async');
        }

        try {
            const result = await asyncAction(convo, guid);
        }
        catch (e) {
            console.error(`Error occurred while in convo for ${actionName}:${guid}`)
        }
        finally {
            delete this.convos[convo];

            if (guid in self.queues) {
                const queue = self.queues;
                queue.close();
                delete self.queues[guid];
            }
        }
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

    close() {
        if (this._reject) {
            this._reject(Error('AsyncQueue closing'));
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

    async expect(timeout=5.0) {
        return timeboxPromise(this.client.getMessageAsync(this.guid), timeout);
    }

    async send(data) {
        return this.client.send({
            ...data,
            action: this.action,
            response_id: this.guid,
        });
    }

    async sendAndExpect(data, timeout=5.0) {
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