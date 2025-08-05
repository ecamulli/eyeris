import streamlit as st
import aiohttp
import asyncio
import json
from datetime import datetime, timedelta, date
import re
import time

# Initialize session state
if "token" not in st.session_state:
    st.session_state.token = None
if "agents_data" not in st.session_state:
    st.session_state.agents_data = None
if "device_list" not in st.session_state:
    st.session_state.device_list = []
if "org_analysis_results" not in st.session_state:
    st.session_state.org_analysis_results = {}

class TokenBucket:
    """Token bucket rate limiter to enforce API rate limits."""
    def __init__(self, rate, capacity):
        self.rate = rate  # Tokens per second
        self.capacity = capacity  # Burst limit
        self.tokens = capacity
        self.last_refill = time.time()

    async def acquire(self):
        """Acquire a token, waiting if necessary."""
        now = time.time()
        # Refill tokens based on elapsed time
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens < 1:
            # Wait until at least one token is available
            await asyncio.sleep((1 - self.tokens) / self.rate)
            self.tokens = min(self.capacity, self.tokens + (time.time() - self.last_refill) * self.rate)
            self.last_refill = time.time()

        self.tokens -= 1
        await asyncio.sleep(0.05)  # Small delay to smooth out bursts

async def authenticate(client_id, client_secret, rate_limiter):
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
        async with rate_limiter.acquire():
            async with session.post(auth_url, data=auth_data, headers=auth_headers) as response:
                if response.status != 200:
                    return None, f"Authentication failed: HTTP {response.status}: {await response.text()}"
                result = await response.json()
                token = result.get("access_token")
                if not token:
                    return None, "No token received"
                return token, None

async def fetch_agents(token, rate_limiter, session):
    """Fetch the list of devices from the agents endpoint."""
    agents_url = 'https://api-v2.7signal.com/eyes/agents'
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }
    async with rate_limiter.acquire():
        async with session.get(agents_url, headers=headers) as response:
            if response.status != 200:
                return None, f"Failed to fetch agents: HTTP {response.status}: {await response.text()}"
            return await response.json(), None

async def post_analysis(session, url, headers, data, analysis_type, rate_limiter, retries=3, backoff_factor=0.75):
    """Helper function to make an async POST request with retry logic."""
    for attempt in range(retries):
        try:
            async with rate_limiter.acquire():
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

async def get_analysis_result(session, url, headers, analysis_type, rate_limiter, retries=3, backoff_factor=0.75):
    """Helper function to make an async GET request with retry logic."""
    for attempt in range(retries):
        try:
            async with rate_limiter.acquire():
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

async def analyze_device(device_id, token, semaphore, rate_limiter, session):
    """Perform all four analyses concurrently for a device with semaphore and rate limiter."""
    async with semaphore:  # Limit concurrent devices
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

        # Concurrent POST requests
        post_tasks = []
        for analysis_type in analysis_types:
            analysis_data = {
                "agentId": device_id,
                "type": analysis_type.upper(),
                "from": str(two_hours_ago),
                "to": str(now)
            }
            post_tasks.append(post_analysis(session, analysis_url, headers, analysis_data, analysis_type, rate_limiter))
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
            get_tasks.append(get_analysis_result(session, result_url, headers, analysis_type, rate_limiter))

        # Concurrent GET requests
        get_results = await asyncio.gather(*get_tasks, return_exceptions=True)
        for analysis_type, result in get_results:
            analysis_results[analysis_type] = result

        return device_id, analysis_results

def summarize_non_compliant_devices(org_analysis_results, device_list):
    """
    Generate a list of devices with less than 100% SLA compliance, showing their issues.
    """
    summary = "**Devices with Less than 100% SLA Compliance**\n\n"
    summary += "----------------------------------\n\n"

    # Map device IDs to display names for readable output
    device_display_names = {
        device_id: f"{nickname if nickname != 'N/A' else name} (ID: {device_id})"
        for name, nickname, device_id in device_list
    }

    # Track devices with issues
    non_compliant_devices = []

    # Process each device's analysis results
    for device_id, (device_name, device_nickname, analysis_results) in org_analysis_results.items():
        display_name = device_display_names.get(device_id, f"{device_name} (ID: {device_id})")
        device_issues = []

        for analysis_type, result in analysis_results.items():
            if result.get("error"):
                device_issues.append(f"{analysis_type}: Failed to retrieve data ({result['error']})")
                continue

            response_text = result["data"].get("response", "")
            num_issues = re.search(r"Number of Issues: (\d+)", response_text)
            main_issue = re.search(r"Main Issue: (.*?)\n", response_text)

            num_issues = int(num_issues.group(1)) if num_issues else 0
            main_issue = main_issue.group(1) if main_issue else "N/A"

            if num_issues > 0:
                device_issues.append(f"{analysis_type}: {main_issue}")

        if device_issues:
            non_compliant_devices.append((display_name, device_issues))

    # Build the summary
    if non_compliant_devices:
        for display_name, issues in non_compliant_devices:
            summary += f"**{display_name}**\n"
            for issue in issues:
                summary += f"- {issue}\n"
            summary += "\n"
    else:
        summary += "All devices are performing well with 100% SLA compliance across all analyses.\n"

    return summary

async def connect_to_api(client_id, client_secret):
    """Helper function to handle authentication and fetching agents."""
    rate_limiter = TokenBucket(rate=5, capacity=15)  # 5 requests/sec, 15 burst
    token, error = await authenticate(client_id, client_secret, rate_limiter)
    if error:
        return None, None, error
    async with aiohttp.ClientSession() as session:
        agents_data, error = await fetch_agents(token, rate_limiter, session)
        if error:
            return None, None, error
        return token, agents_data, None

# Streamlit app
st.title("7SIGNAL Eyeris AI Organization Analysis")

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
        token, agents_data, error = asyncio.run(connect_to_api(client_id, client_secret))
        if error:
            st.error(error)
        else:
            st.session_state.token = token
            st.session_state.agents_data = agents_data
            # Get today's date for filtering
            today = date.today()
            # Build device list, only include licensed devices with lastTestSeen today
            st.session_state.device_list = [
                (agent.get("name", "N/A"), agent.get("nickname", "N/A"), agent.get("id"))
                for agent in agents_data.get("results", [])
                if agent.get("isLicensed", False) and agent.get("lastTestSeen") and
                datetime.fromtimestamp(agent.get("lastTestSeen") / 1000).date() == today
            ]
            if st.session_state.device_list:
                st.success(f"Connected! Found {len(st.session_state.device_list)} licensed devices with tests seen today.")
            else:
                st.warning("No licensed devices with tests seen today found. Please check your account or API response.")

# Organization-wide analysis
if st.session_state.token and st.session_state.device_list:
    st.header("Analyze All Devices")
    org_analyze_button = st.button("Run Organization Analysis")

    if org_analyze_button:
        with st.spinner("Running organization-wide analysis..."):
            st.session_state.org_analysis_results = {}
            progress_bar = st.progress(0)
            total_devices = len(st.session_state.device_list)
            semaphore = asyncio.Semaphore(1)  # Limit to 1 concurrent device (up to 8 requests)
            rate_limiter = TokenBucket(rate=5, capacity=15)  # 5 requests/sec, 15 burst

            async def run_all_analyses():
                async with aiohttp.ClientSession() as session:
                    tasks = [
                        analyze_device(device_id, st.session_state.token, semaphore, rate_limiter, session)
                        for _, _, device_id in st.session_state.device_list
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for i, (device_id, analysis_results) in enumerate(results):
                        if not isinstance(analysis_results, Exception):
                            device_name, device_nickname, _ = st.session_state.device_list[i]
                            st.session_state.org_analysis_results[device_id] = (device_name, device_nickname, analysis_results)
                        progress_bar.progress((i + 1) / total_devices)

            asyncio.run(run_all_analyses())

            st.header("Non-Compliant Devices")
            org_summary = summarize_non_compliant_devices(st.session_state.org_analysis_results, st.session_state.device_list)
            st.markdown(org_summary)
