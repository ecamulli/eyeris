import streamlit as st
import aiohttp
import asyncio
import json
from datetime import datetime, timedelta
import re

# Initialize session state
if "token" not in st.session_state:
    st.session_state.token = None
if "agents_data" not in st.session_state:
    st.session_state.agents_data = None
if "device_list" not in st.session_state:
    st.session_state.device_list = []

async def authenticate(client_id, client_secret):
    """Authenticate with the 7SIGNAL API and return token."""
    auth_url = 'https://api-v2.7signal.com/oauth2/token'
    auth_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }
    auth_headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(auth_url, data=auth_data, headers=auth_headers) as response:
            if response.status != 200:
                return None, f"Authentication failed: HTTP {response.status}: {await response.text()}"
            result = await response.json()
            token = result.get("access_token")
            if not token:
                return None, "No token received"
            return token, None

async def fetch_agents(token):
    """Fetch the list of devices from the agents endpoint."""
    agents_url = 'https://api-v2.7signal.com/eyes/agents'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(agents_url, headers=headers) as response:
            if response.status != 200:
                return None, f"Failed to fetch agents: HTTP {response.status}: {await response.text()}"
            return await response.json(), None

async def post_analysis(session, url, headers, data, analysis_type, retries=3, backoff_factor=1):
    """Helper function to make an async POST request with retry logic."""
    for attempt in range(retries):
        try:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    return analysis_type, await response.json()
                elif response.status == 429:  # Rate limit
                    wait_time = backoff_factor * (2 ** attempt)
                    st.warning(f"Rate limit hit for {analysis_type}, retrying after {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    return analysis_type, {"error": f"HTTP {response.status}: {await response.text()}"}
        except aiohttp.ClientError as e:
            if attempt == retries - 1:
                return analysis_type, {"error": str(e)}
            await asyncio.sleep(backoff_factor * (2 ** attempt))
    return analysis_type, {"error": "Max retries reached"}

async def get_analysis_result(session, url, headers, analysis_type, retries=3, backoff_factor=1):
    """Helper function to make an async GET request with retry logic."""
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return analysis_type, {"data": await response.json()}
                elif response.status == 429:  # Rate limit
                    wait_time = backoff_factor * (2 ** attempt)
                    st.warning(f"Rate limit hit for {analysis_type} result, retrying after {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    return analysis_type, {"error": f"HTTP {response.status}: {await response.text()}"}
        except aiohttp.ClientError as e:
            if attempt == retries - 1:
                return analysis_type, {"error": str(e)}
            await asyncio.sleep(backoff_factor * (2 ** attempt))
    return analysis_type, {"error": "Max retries reached"}

async def analyze_device(device_id, token):
    """Perform all four analyses concurrently for a device."""
    analysis_types = ["Roaming", "Coverage", "Congestion", "Interference"]
    analysis_results = {}
    analysis_url = 'https://api-v2.7signal.com/eyeris/agents/client-analysis'

    # Calculate timestamps once
    now = int(datetime.now().timestamp() * 1000)
    two_hours_ago = int((datetime.now() - timedelta(hours=2)).timestamp() * 1000)

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    async with aiohttp.ClientSession() as session:
        # Concurrent POST requests
        post_tasks = []
        for analysis_type in analysis_types:
            analysis_data = {
                "agentId": device_id,
                "type": analysis_type.upper(),
                "from": str(two_hours_ago),
                "to": str(now)
            }
            post_tasks.append(post_analysis(session, analysis_url, headers, analysis_data, analysis_type))
        post_results = await asyncio.gather(*post_tasks, return_exceptions=True)

        # Process POST results
        get_tasks = []
        for analysis_type, result in post_results:
            if result.get("error"):
                analysis_results[analysis_type] = result
                continue
            request_id = result.get("requestId")
            request_queue_id = result.get("requestQueueId")
            if not request_id or not request_queue_id:
                analysis_results[analysis_type] = {"error": "Invalid analysis response"}
                continue
            result_url = f"https://api-v2.7signal.com/eyeris/agents/client-analysis/{request_id}?requestQueueId={request_queue_id}"
            get_tasks.append(get_analysis_result(session, result_url, headers, analysis_type))

        # Concurrent GET requests
        get_results = await asyncio.gather(*get_tasks, return_exceptions=True)
        for analysis_type, result in get_results:
            analysis_results[analysis_type] = result

    return analysis_results

def summarize_analysis_results(device_name, device_nickname, analysis_results):
    """
    Generate a simplified summary from the four analysis responses.
    Parses the 'response' field to extract key issues, metrics, and recommendations.
    """
    summary = f"**Summary for Device: {device_name} (Nickname: {device_nickname})**\n\n"
    summary += "----------------------------------\n\n"

    issues_found = []
    metrics_summary = []
    recommendations = {"device_side": [], "network_side": []}
    failed_analyses = []

    for analysis_type, result in analysis_results.items():
        if result.get("error"):
            failed_analyses.append(f"{analysis_type}: Failed to retrieve data ({result['error']})")
            continue

        response_text = result["data"].get("response", "")
        # Parse key information from response text
        num_issues = re.search(r"Number of Issues: (\d+)", response_text)
        major_issues = re.search(r"Major Issues: (.*?)\n", response_text)
        total_impact = re.search(r"Total Impact: (.*?)\n", response_text)
        main_issue = re.search(r"Main Issue: (.*?)\n", response_text)

        num_issues = int(num_issues.group(1)) if num_issues else 0
        major_issues = major_issues.group(1) if major_issues else "None"
        total_impact = total_impact.group(1) if total_impact else "N/A"
        main_issue = main_issue.group(1) if main_issue else "N/A"

        # Summarize issues
        if num_issues > 0:
            issues_found.append(f"{analysis_type}: {main_issue} (Impact: {total_impact})")
        else:
            issues_found.append(f"{analysis_type}: No significant issues (100% SLA compliance)")

        # Extract key metrics
        if analysis_type == "Roaming":
            metrics = re.findall(r"Channel utilization.*?\n|Signal strength.*?dBm|Client counts.*?\n", response_text)
            metrics_summary.append(f"{analysis_type}: {', '.join([m.strip() for m in metrics if m])}")
        elif analysis_type == "Coverage":
            metrics = re.findall(r"Signal strength.*?dBm|7MCS values.*?[,\.\n]", response_text)
            metrics_summary.append(f"{analysis_type}: {', '.join([m.strip() for m in metrics if m])}")
        elif analysis_type == "Congestion":
            metrics = re.findall(r"Channel utilization.*?[,\.\n]|Signal strength.*?dBm|Client counts.*?\n", response_text)
            metrics_summary.append(f"{analysis_type}: {', '.join([m.strip() for m in metrics if m])}")
        elif analysis_type == "Interference":
            metrics = re.findall(r"Co-Channel Interference.*?[,\.\n]|Channel utilization.*?[,\.\n]|Signal strength.*?dBm", response_text)
            metrics_summary.append(f"{analysis_type}: {', '.join([m.strip() for m in metrics if m])}")

        # Extract recommendations
        device_fixes = re.findall(r"Device-side fixes.*?:(.*?)(?=(Network-side fixes|Note:|$))", response_text, re.DOTALL)
        network_fixes = re.findall(r"Network-side fixes.*?:(.*?)(?=(Device-side fixes|Note:|$))", response_text, re.DOTALL)
        if device_fixes:
            recommendations["device_side"].extend([f.strip() for f in device_fixes[0][0].split("\n") if f.strip()])
        if network_fixes:
            recommendations["network_side"].extend([f.strip() for f in network_fixes[0][0].split("\n") if f.strip()])

    # Build the summary
    summary += "**Performance Overview**\n"
    for issue in issues_found:
        summary += f"- {issue}\n"

    summary += "\n**Key Metrics**\n"
    for metric in metrics_summary:
        summary += f"- {metric}\n"

    # Deduplicate and prioritize recommendations
    unique_device_fixes = list(dict.fromkeys([f for f in recommendations["device_side"] if f]))
    unique_network_fixes = list(dict.fromkeys([f for f in recommendations["network_side"] if f]))
    if unique_device_fixes or unique_network_fixes:
        summary += "\n**Recommended Actions**\n"
        if unique_device_fixes:
            summary += "**Device-Side**\n" + "\n".join([f"  - {f}" for f in unique_device_fixes[:3]]) + "\n"
        if unique_network_fixes:
            summary += "**Network-Side**\n" + "\n".join([f"  - {f}" for f in unique_network_fixes[:3]]) + "\n"

    if failed_analyses:
        summary += "\n**Failed Analyses**\n" + "\n".join([f"- {f}" for f in failed_analyses]) + "\n"

    # Overall assessment
    successful_analyses = len([k for k, v in analysis_results.items() if not v.get("error")])
    total_issues = sum([int(re.search(r"Number of Issues: (\d+)", analysis_results[k]["data"].get("response", "")).group(1)) 
                        for k in analysis_results if not analysis_results[k].get("error") and re.search(r"Number of Issues: (\d+)", analysis_results[k]["data"].get("response", ""))])
    if successful_analyses == 4 and total_issues == 0:
        summary += "\n**Overall**: The device is performing well with no issues across all analyses.\n"
    elif successful_analyses > 0:
        summary += f"\n**Overall**: {successful_analyses} of 4 analyses completed successfully. {total_issues} issue(s) found, check recommendations for fixes.\n"
    else:
        summary += "\n**Overall**: No analyses completed successfully. Please check API connectivity or device status.\n"

    return summary

# Streamlit app
st.title("7SIGNAL Network Analysis App")

# Authentication section
st.header("Authenticate with 7SIGNAL API")
col1, col2 = st.columns(2)
with col1:
    client_id = st.text_input("Client ID", type="password")
with col2:
    client_secret = st.text_input("Client Secret", type="password")
connect_button = st.button("Connect")

if connect_button and client_id and client_secret:
    with st.spinner("Authenticating..."):
        token, error = asyncio.run(authenticate(client_id, client_secret))
        if error:
            st.error(error)
        else:
            st.session_state.token = token
            agents_data, error = asyncio.run(fetch_agents(token))
            if error:
                st.error(error)
            else:
                st.session_state.agents_data = agents_data
                # Build device list for dropdown, only include licensed devices
                st.session_state.device_list = [
                    (agent.get("name", "N/A"), agent.get("nickname", "N/A"), agent.get("id"))
                    for agent in agents_data.get("results", [])
                    if agent.get("isLicensed", False)
                ]
                if st.session_state.device_list:
                    st.success(f"Connected! Found {len(st.session_state.device_list)} licensed devices.")
                else:
                    st.warning("No licensed devices found. Please check your account or API response.")

# Device selection and analysis
if st.session_state.token and st.session_state.device_list:
    st.header("Analyze Device")
    # Create display names for dropdown (show nickname if available, else name)
    display_names = [
        f"{nickname if nickname != 'N/A' else name} (ID: {device_id})"
        for name, nickname, device_id in st.session_state.device_list
    ]
    selected_device = st.selectbox("Select Device", display_names)
    analyze_button = st.button("Run Analysis")

    if analyze_button:
        # Find selected device ID
        selected_index = display_names.index(selected_device)
        device_name, device_nickname, device_id = st.session_state.device_list[selected_index]

        with st.spinner("Running analyses..."):
            analysis_results = asyncio.run(analyze_device(device_id, st.session_state.token))
            st.header("Analysis Results")
            summary = summarize_analysis_results(device_name, device_nickname, analysis_results)
            st.markdown(summary)
