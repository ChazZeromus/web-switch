import Client, { timeboxPromise } from './client';


const URL = 'ws://localhost:8765';

async function foo() {
    const client = new Client(URL + '/somechannel/someroom/');

    await client.convo('whoami', async (convo, guid) => {
        const data = await convo.sendAndExpect({action: 'whoami'});
        console.log('Reply:', data);
    });

    await client.close();
}

foo().catch(data => console.error('whoops', data));


