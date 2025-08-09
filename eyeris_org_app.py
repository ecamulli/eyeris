# ... (Previous code unchanged: imports, TokenBucket, API functions, etc.) ...

# Streamlit app
st.title("7SIGNAL Eyeris AI Organization Analysis")

# Authentication section (unchanged)
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
            today = date.today()
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
            semaphore = asyncio.Semaphore(1)
            rate_limiter = TokenBucket(rate=5, capacity=15)

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

            # Generate HTML dashboard
            def generate_html_dashboard(org_analysis_results, device_list):
                html_content = """
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>7SIGNAL Eyeris Non-Compliant Devices Dashboard</title>
                    <script src="https://cdn.tailwindcss.com"></script>
                </head>
                <body class="bg-gray-100 font-sans">
                    <div class="container mx-auto p-4">
                        <h1 class="text-3xl font-bold text-center text-gray-800 mb-6">7SIGNAL Eyeris Non-Compliant Devices</h1>
                        <div class="bg-white shadow-md rounded-lg p-6">
                            <h2 class="text-xl font-semibold text-gray-700 mb-4">Devices with Issues</h2>
                            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                """

                device_display_names = {
                    device_id: f"{nickname if nickname != 'N/A' else name} (ID: {device_id})"
                    for name, nickname, device_id in device_list
                }
                non_compliant_devices = []

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

                if non_compliant_devices:
                    for display_name, issues in non_compliant_devices:
                        html_content += """
                        <div class="bg-red-50 p-4 rounded-lg shadow">
                            <h3 class="text-lg font-medium text-red-800">{}</h3>
                            <ul class="list-disc list-inside text-gray-700">
                        """.format(display_name)
                        for issue in issues:
                            html_content += f'<li>{issue}</li>'
                        html_content += """
                            </ul>
                        </div>
                        """
                else:
                    html_content += """
                    <div class="bg-green-50 p-4 rounded-lg shadow">
                        <h3 class="text-lg font-medium text-green-800">All Devices Compliant</h3>
                        <p class="text-gray-700">All devices are performing well with 100% SLA compliance across all analyses.</p>
                    </div>
                    """

                html_content += """
                            </div>
                        </div>
                    </div>
                </body>
                </html>
                """
                return html_content

            # Generate HTML content
            html_dashboard = generate_html_dashboard(st.session_state.org_analysis_results, st.session_state.device_list)

            # Download button for HTML dashboard
            st.download_button(
                label="Download Dashboard",
                data=html_dashboard,
                file_name=f"7signal_eyeris_dashboard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                mime="text/html"
            )

            # Display dashboard in Streamlit
            st.subheader("Preview Dashboard")
            st.components.v1.html(html_dashboard, height=600, scrolling=True)
