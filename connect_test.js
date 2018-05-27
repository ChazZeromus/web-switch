const WebSocket = require('ws');
const _ = require('lodash');
const EventEmitter = require('events');

const URL = 'ws://localhost:8765';

const create = url => {
    let ws = new WebSocket(url);

    ws.onmessage = console.log;

    return ws;
};

class Client extends EventEmitter {
    constructor(url) {
        super();

        this.ws = new WebSocket(url);

        this.ws.addEventListener('message', this.handleMessage.bind(this));
        this.ws.addEventListener('open', this.handleOpen.bind(this));
        this.ws.addEventListener('close', this.handleClose.bind(this));
        this.ws.addEventListener('error', this.handleError.bind(this));
    }

    handleOpen(data) {
        console.info('Client socket opened, state:', this.ws.readyState);
        this.emit('open', data);
    }

    handleError(data) {
        console.error('Client socket error occurred');
        this.emit('error', data);
    }

    handleClose(data) {
        console.info('Client Socket closed', data);
        this.emit('close', data);
    }

    handleMessage(data) {
        this.emit('message', data);
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

    waitForAsync(event, filter, timeout, resolveResponse=true, rejectResponse=false) {
        return new Promise((resolve, reject) => {
            let timeoutId;

            const cleanUp = () => {
                this.removeListener(event, filterCallback);
                clearTimeout(timeoutId);
            };

            const filterCallback = data => {
                try {
                    if (filter !== true || (_.isFunction(filter) && !filter(data))) {
                        return;
                    }
                }
                catch (e) {
                    reject(e);
                }

                cleanUp();
                this.removeListener('error', errorCallback);

                resolve(resolveResponse);
            };

            const rejectCallback = data => {
                data = data || 'Timed out while waiting for response';
                clearTimeout(timeoutId);

                cleanUp();
                this.removeListener('error', errorCallback);

                const rejectData = _.isFunction(rejectResponse) ? rejectResponse(data) : rejectResponse;

                reject(rejectData);
            };

            const errorCallback = (data) => {
                // console.error('Error occurred while waiting for response', data);
                rejectCallback(data.message);
            };

            timeoutId = setTimeout(rejectCallback, timeout);

            this.on(event, filterCallback);
            this.once('error', errorCallback);
        });
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

async function foo() {
    const client = new Client(URL + '/somechannel/someroom/');
    await client.send({message: 'hello!'});
    await client.waitForReply(true);
    await client.close();
}

foo().catch(data => console.error('whoops', data));


