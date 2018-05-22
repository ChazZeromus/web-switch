const WebSocket = require('ws');
const _ = require('lodash');

const URL = 'ws://localhost:8765';

const create = url => {
    let ws = new WebSocket(url);

    ws.onmessage = console.log;

    return ws;
};


_.range(40).forEach(() => {
    const ws = create(URL);

    ws.onopen = () => {
        ws.send('{}');
        ws.send('{}');
    };
});
