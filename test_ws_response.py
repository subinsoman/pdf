#!/usr/bin/env python3
"""
Test script to verify WebSocket response parsing logic
"""

import json

# Actual WebSocket response (top-level output)
sample_response = """{
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
}"""

def extract_output(resp_str):
    """Extract output from WebSocket response"""
    try:
        resp_json = json.loads(resp_str)
        print("✅ Parsed JSON successfully")
        print(f"Full response: {json.dumps(resp_json, indent=2)}")
        
        # Extract output from the response structure
        if isinstance(resp_json, dict):
            # Try direct output field first (top-level)
            output = resp_json.get("output")
            
            # If not found, try message.output (nested)
            if not output:
                message = resp_json.get("message", {})
                if isinstance(message, dict):
                    output = message.get("output")
            
            if output:
                print(f"\n✅ Extracted output: {output}")
                return output
            else:
                print("❌ No 'output' field found")
        else:
            print("❌ Response is not a dict")
        
        return resp_str
    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        return resp_str

if __name__ == "__main__":
    print("Testing WebSocket response parsing...\n")
    result = extract_output(sample_response)
    print(f"\n📤 Final output to display: {result}")
