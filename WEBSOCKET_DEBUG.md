# WebSocket Debugging Guide

## Changes Made

### 1. Added WebSocket Client Library
- Added `websocket-client>=1.6.0` to `requirements.txt`
- This library is required for WebSocket communication with the Aarya backend

### 2. Enhanced Error Handling
- Added detailed error messages in `_ws_send_message()` function
- Shows specific error types and messages when WebSocket connection fails
- Optional debug mode with verbose logging

### 3. Workflow ID Extraction
- Updated `_get_workflow_id()` to automatically extract `workflow_id` from WebSocket URL query parameters
- Your current URL: `wss://n8n.6dtech.co.in:8090?client_id=tes44t123&workflow_id=c9a8c60b-a696-4eeb-bede-ef207a7cf092`
- The function will extract: `c9a8c60b-a696-4eeb-bede-ef207a7cf092`

### 4. Reconnect Button
- Added a "🔄 Reconnect" button in the Aarya page UI
- Clears the current WebSocket session and creates a new one
- Useful when:
  - Connection is stuck or unresponsive
  - You want to start a fresh conversation
  - WebSocket server was restarted

### 5. Session Status Indicator
- Shows connection status:
  - 🟢 Connected • Session: [session_id]
  - ⚪ Not connected • Send a message to connect

## Testing Steps

1. **Install the new dependency:**
   ```bash
   pip install websocket-client>=1.6.0
   ```

2. **Run the application:**
   ```bash
   streamlit run app.py
   ```

3. **Navigate to Aarya page:**
   - Select a knowledge base
   - You'll see the session status indicator

4. **Send a test message:**
   - Type a question in the chat input
   - The app will connect to WebSocket automatically
   - You'll see the Aarya response extracted from `output` field
   - If connection fails, you'll see an error message

5. **Use Reconnect button:**
   - Click "🔄 Reconnect" to reset the session
   - New session ID will be created on next message
   - Useful if connection is stuck or you want a fresh start

## Common Issues & Solutions

### Issue: "WebSocket library not available"
**Solution:** Install the library:
```bash
pip install websocket-client
```

### Issue: Connection timeout
**Possible causes:**
- WebSocket server is down
- Firewall blocking the connection
- Incorrect URL or port

**Debug steps:**
1. Check if the WebSocket server is running at `wss://n8n.6dtech.co.in:8090`
2. Test connection with a WebSocket client tool
3. Verify SSL certificate is valid (currently using `CERT_NONE` to bypass)

### Issue: "Connection refused"
**Possible causes:**
- Server not accepting connections
- Wrong port number
- Server requires authentication

### Issue: No response received
**Possible causes:**
- Server doesn't recognize the payload format
- Wrong workflow_id or client_id
- Server timeout

## Payload Structure

### Request (Sent to WebSocket)
The application sends this payload to the WebSocket:
```json
{
  "action": "sendMessage",
  "sessionId": "session_[unique_id]",
  "route": "general",
  "chatInput": "[user message]",
  "msg_id": "msg_id-[timestamp]",
  "knowledge_name": "[selected product name]",
  "name": "[selected product name]",
  "type": "message",
  "message": "[user message]",
  "timestamp": "2025-11-17T12:00:00.000Z",
  "client_id": "selfcare_[timestamp]_[random]",
  "workflow_id": "c9a8c60b-a696-4eeb-bede-ef207a7cf092"
}
```

### Response (Received from WebSocket)
The WebSocket server returns a response in this format:
```json
{
  "message": {
    "output": "It seems like you didn't describe an issue. Please provide more details about the problem you're facing.",
    "event": {
      "type": "",
      "param0": ""
    },
    "extra": [
      {
        "type": "",
        "content": {}
      },
      {
        "type": "",
        "options": []
      }
    ]
  }
}
```

**The application extracts `message.output`** and displays it as the Aarya response to the user.

## Response Parsing

The application automatically extracts the `output` field from the WebSocket JSON response and displays it as the Aarya answer. The full JSON structure is parsed but only the user-facing message is shown in the chat.

## Next Steps

1. Send a test message to Aarya
2. If connection fails, check the error message
3. Use the 🔄 Reconnect button if needed
4. Verify the WebSocket server is configured to accept the payload format shown above
