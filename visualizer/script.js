async function updateState() {
    try {
        const response = await fetch('state.json?t=' + Date.now());
        const data = await response.json();
        const state = data.state;
        const body = document.body;
        const statusText = document.getElementById('status-text');
        const dataContent = document.getElementById('data-content');

        // Update body class for animations
        body.className = state;

        // Update status text
        switch(state) {
            case 'listening':
                statusText.innerText = 'LISTENING...';
                statusText.style.color = '#fff';
                break;
            case 'speaking':
                statusText.innerText = 'TRANSMITTING...';
                statusText.style.color = '#00d2ff';
                break;
            default:
                statusText.innerText = 'STANDING BY';
                statusText.style.color = '#00d2ff';
        }

        // Randomly flicker CPU/MEM bars for realism
        const cpu = Math.floor(Math.random() * 30) + 40;
        const mem = Math.floor(Math.random() * 20) + 60;
        document.getElementById('cpu-bar').style.width = cpu + '%';
        document.getElementById('mem-bar').style.width = mem + '%';

    } catch (e) {
        // console.error('State sync error:', e);
    }
}

// Poll every 100ms for "Zero-Lag" responsiveness
setInterval(updateState, 100);

// Data Feed Animation
const feedLines = [
    "Uplink established...",
    "Neural sensors active.",
    "Bypassing mainframe...",
    "Tactical HUD initialized.",
    "Ready for your command, Sir."
];
let lineIdx = 0;

function updateFeed() {
    const content = document.getElementById('data-content');
    if (lineIdx < feedLines.length) {
        const p = document.createElement('div');
        p.innerText = "> " + feedLines[lineIdx];
        p.style.opacity = "0";
        p.style.transition = "opacity 0.5s";
        content.appendChild(p);
        setTimeout(() => p.style.opacity = "1", 10);
        lineIdx++;
    }
}

setInterval(updateFeed, 2000);
