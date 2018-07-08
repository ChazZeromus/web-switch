import Client from './client';

const URL = 'ws://localhost:8765';

async function foo() {
    const client = new Client(URL + '/somechannel/someroom/');
    await client.send({action: 'whoami'});
    console.log('Reply:', await client.waitForReply(true));
    await client.close();
}

foo().catch(data => console.error('whoops', data));


