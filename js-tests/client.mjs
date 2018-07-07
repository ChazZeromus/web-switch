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
