#!/usr/bin/env python3
"""
FlareProx - Simple URL Redirection via Cloudflare Workers
Redirect all traffic through Cloudflare Workers for any provided URL
"""

import argparse
import getpass
import json
import os
import random
import requests
import string
import time
from typing import Dict, List, Optional


class FlareProxError(Exception):
    """Custom exception for FlareProx-specific errors."""
    pass


class CloudflareManager:
    """Manages Cloudflare Worker deployments for FlareProx."""

    def __init__(self, api_token: str, account_id: str, zone_id: Optional[str] = None):
        self.api_token = api_token
        self.account_id = account_id
        self.zone_id = zone_id
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        self._account_subdomain = None

    @property
    def worker_subdomain(self) -> str:
        """Get the worker subdomain for workers.dev URLs."""
        if self._account_subdomain:
            return self._account_subdomain

        # Try to get configured subdomain
        url = f"{self.base_url}/accounts/{self.account_id}/workers/subdomain"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                subdomain = data.get("result", {}).get("subdomain")
                if subdomain:
                    self._account_subdomain = subdomain
                    return subdomain
        except requests.RequestException:
            pass

        # Fallback: use account ID as subdomain
        self._account_subdomain = self.account_id.lower()
        return self._account_subdomain

    def _generate_worker_name(self) -> str:
        """Generate a unique worker name."""
        timestamp = str(int(time.time()))
        random_suffix = ''.join(random.choices(string.ascii_lowercase, k=6))
        return f"flareprox-{timestamp}-{random_suffix}"

    def _get_worker_script(self) -> str:
        """Return the optimized Cloudflare Worker script."""
        return '''/**
 * FlareProx - Cloudflare Worker URL Redirection Script
 */
addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request))
})

async function handleRequest(request) {
  try {
    const url = new URL(request.url)
    const targetUrl = getTargetUrl(url, request.headers)

    if (!targetUrl) {
      return createErrorResponse('No target URL specified', {
        usage: {
          query_param: '?url=https://example.com',
          header: 'X-Target-URL: https://example.com',
          path: '/https://example.com'
        }
      }, 400)
    }

    let targetURL
    try {
      targetURL = new URL(targetUrl)
    } catch (e) {
      return createErrorResponse('Invalid target URL', { provided: targetUrl }, 400)
    }

    // Build target URL with filtered query parameters
    const targetParams = new URLSearchParams()
    for (const [key, value] of url.searchParams) {
      if (!['url', '_cb', '_t'].includes(key)) {
        targetParams.append(key, value)
      }
    }
    if (targetParams.toString()) {
      targetURL.search = targetParams.toString()
    }

    // Create proxied request
    const proxyRequest = createProxyRequest(request, targetURL)
    const response = await fetch(proxyRequest)

    // Process and return response
    return createProxyResponse(response, request.method)

  } catch (error) {
    return createErrorResponse('Proxy request failed', {
      message: error.message,
      timestamp: new Date().toISOString()
    }, 500)
  }
}

function getTargetUrl(url, headers) {
  // Priority: query param > header > path
  let targetUrl = url.searchParams.get('url')

  if (!targetUrl) {
    targetUrl = headers.get('X-Target-URL')
  }

  if (!targetUrl && url.pathname !== '/') {
    const pathUrl = url.pathname.slice(1)
    if (pathUrl.startsWith('http')) {
      targetUrl = pathUrl
    }
  }

  return targetUrl
}

function createProxyRequest(request, targetURL) {
  const proxyHeaders = new Headers()
  const allowedHeaders = [
    'accept', 'accept-language', 'accept-encoding', 'authorization',
    'cache-control', 'content-type', 'origin', 'referer', 'user-agent'
  ]

  // Copy allowed headers
  for (const [key, value] of request.headers) {
    if (allowedHeaders.includes(key.toLowerCase())) {
      proxyHeaders.set(key, value)
    }
  }

  proxyHeaders.set('Host', targetURL.hostname)

  // Set X-Forwarded-For header
  const customXForwardedFor = request.headers.get('X-My-X-Forwarded-For')
  if (customXForwardedFor) {
    proxyHeaders.set('X-Forwarded-For', customXForwardedFor)
  } else {
    proxyHeaders.set('X-Forwarded-For', generateRandomIP())
  }

  return new Request(targetURL.toString(), {
    method: request.method,
    headers: proxyHeaders,
    body: ['GET', 'HEAD'].includes(request.method) ? null : request.body
  })
}

function createProxyResponse(response, requestMethod) {
  const responseHeaders = new Headers()

  // Copy response headers (excluding problematic ones)
  for (const [key, value] of response.headers) {
    if (!['content-encoding', 'content-length', 'transfer-encoding'].includes(key.toLowerCase())) {
      responseHeaders.set(key, value)
    }
  }

  // Add CORS headers
  responseHeaders.set('Access-Control-Allow-Origin', '*')
  responseHeaders.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD')
  responseHeaders.set('Access-Control-Allow-Headers', '*')

  if (requestMethod === 'OPTIONS') {
    return new Response(null, { status: 204, headers: responseHeaders })
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders
  })
}

function createErrorResponse(error, details, status) {
  return new Response(JSON.stringify({ error, ...details }), {
    status,
    headers: { 'Content-Type': 'application/json' }
  })
}

function generateRandomIP() {
  return [1, 2, 3, 4].map(() => Math.floor(Math.random() * 255) + 1).join('.')
}'''

    def create_deployment(self, name: Optional[str] = None) -> Dict:
        """Deploy a new Cloudflare Worker."""
        if not name:
            name = self._generate_worker_name()

        script_content = self._get_worker_script()
        url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{name}"

        files = {
            'metadata': (None, json.dumps({
                "body_part": "script",
                "main_module": "worker.js"
            })),
            'script': ('worker.js', script_content, 'application/javascript')
        }

        headers = {"Authorization": f"Bearer {self.api_token}"}

        try:
            response = requests.put(url, headers=headers, files=files, timeout=60)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FlareProxError(f"Failed to create worker: {e}")

        worker_data = response.json()

        # Enable subdomain
        subdomain_url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{name}/subdomain"
        try:
            requests.post(subdomain_url, headers=self.headers, json={"enabled": True}, timeout=30)
        except requests.RequestException:
            pass  # Subdomain enabling is not critical

        worker_url = f"https://{name}.{self.worker_subdomain}.workers.dev"

        return {
            "name": name,
            "url": worker_url,
            "created_at": time.strftime('%Y-%m-%d %H:%M:%S'),
            "id": worker_data.get("result", {}).get("id", name)
        }

    def list_deployments(self) -> List[Dict]:
        """List all FlareProx deployments."""
        url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts"

        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FlareProxError(f"Failed to list workers: {e}")

        data = response.json()
        workers = []

        for script in data.get("result", []):
            name = script.get("id", "")
            if name.startswith("flareprox-"):
                workers.append({
                    "name": name,
                    "url": f"https://{name}.{self.worker_subdomain}.workers.dev",
                    "created_at": script.get("created_on", "unknown")
                })

        return workers

    def test_deployment(self, deployment_url: str, target_url: str, method: str = "GET") -> Dict:
        """Test a deployment endpoint."""
        test_url = f"{deployment_url}?url={target_url}"

        try:
            response = requests.request(method, test_url, timeout=30)
            return {
                "success": True,
                "status_code": response.status_code,
                "response_length": len(response.content),
                "headers": dict(response.headers)
            }
        except requests.RequestException as e:
            return {
                "success": False,
                "error": str(e)
            }

    def cleanup_all(self) -> None:
        """Delete all FlareProx workers."""
        workers = self.list_deployments()

        for worker in workers:
            url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{worker['name']}"
            try:
                response = requests.delete(url, headers=self.headers, timeout=30)
                if response.status_code in [200, 404]:
                    print(f"Deleted worker: {worker['name']}")
                else:
                    print(f"Could not delete worker: {worker['name']}")
            except requests.RequestException:
                print(f"Error deleting worker: {worker['name']}")


class FlareProx:
    """Main FlareProx manager class."""

    def __init__(self, config_file: Optional[str] = None):
        self.config = self._load_config(config_file)
        self.cloudflare = self._setup_cloudflare()
        self.endpoints_file = "flareprox_endpoints.json"
        self._ensure_config_file_exists()

    def _load_config(self, config_file: Optional[str] = None) -> Dict:
        """Load configuration from file."""
        config = {"cloudflare": {}}

        # Try specified config file
        if config_file and os.path.exists(config_file):
            config = self._load_config_file(config_file, config)

        # Try default config files
        default_configs = [
            "flareprox.json",
            "cloudproxy.json",  # Legacy support
            os.path.expanduser("~/.flareprox.json")
        ]

        for default_config in default_configs:
            if os.path.exists(default_config):
                config = self._load_config_file(default_config, config)
                break

        return config

    def _load_config_file(self, config_path: str, config: Dict) -> Dict:
        """Load configuration from a JSON file."""
        try:
            with open(config_path, 'r') as f:
                file_config = json.load(f)

            if "cloudflare" in file_config and not config["cloudflare"]:
                config["cloudflare"].update(file_config["cloudflare"])
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load config file {config_path}: {e}")

        return config

    def _setup_cloudflare(self) -> Optional[CloudflareManager]:
        """Setup Cloudflare manager if credentials are available."""
        cf_config = self.config.get("cloudflare", {})
        api_token = cf_config.get("api_token")
        account_id = cf_config.get("account_id")

        if api_token and account_id:
            return CloudflareManager(
                api_token=api_token,
                account_id=account_id,
                zone_id=cf_config.get("zone_id")
            )
        return None

    def _ensure_config_file_exists(self) -> None:
        """Create a default config file if none exists."""
        config_files = ["flareprox.json", os.path.expanduser("~/.flareprox.json")]

        # Check if any config file exists
        config_exists = any(os.path.exists(f) for f in config_files)

        if not config_exists:
            # Don't create a default config automatically
            # Let the user run 'python3 flareprox.py config' to set up
            pass

    @property
    def is_configured(self) -> bool:
        """Check if FlareProx is properly configured."""
        return self.cloudflare is not None

    def _save_endpoints(self, endpoints: List[Dict]) -> None:
        """Save endpoints to local file."""
        try:
            with open(self.endpoints_file, 'w') as f:
                json.dump(endpoints, f, indent=2)
        except IOError as e:
            print(f"Warning: Could not save endpoints: {e}")

    def _load_endpoints(self) -> List[Dict]:
        """Load endpoints from local file."""
        if os.path.exists(self.endpoints_file):
            try:
                with open(self.endpoints_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def sync_endpoints(self) -> List[Dict]:
        """Sync local endpoints with remote deployments."""
        if not self.cloudflare:
            return []

        try:
            endpoints = self.cloudflare.list_deployments()
            self._save_endpoints(endpoints)
            return endpoints
        except FlareProxError as e:
            print(f"Warning: Could not sync endpoints: {e}")
            return self._load_endpoints()

    def create_proxies(self, count: int = 1) -> Dict:
        """Create proxy endpoints."""
        if not self.cloudflare:
            raise FlareProxError("FlareProx not configured")

        print(f"\nCreating {count} FlareProx endpoint{'s' if count != 1 else ''}...")

        results = {"created": [], "failed": 0}

        for i in range(count):
            try:
                endpoint = self.cloudflare.create_deployment()
                results["created"].append(endpoint)
                print(f"  [{i+1}/{count}] {endpoint['name']} -> {endpoint['url']}")
            except FlareProxError as e:
                print(f"  Failed to create endpoint {i+1}: {e}")
                results["failed"] += 1

        # Update local cache
        self.sync_endpoints()

        total_created = len(results["created"])
        print(f"\nCreated: {total_created}, Failed: {results['failed']}")

        return results

    def list_proxies(self) -> List[Dict]:
        """List all proxy endpoints."""
        endpoints = self.sync_endpoints()

        if not endpoints:
            print("No FlareProx endpoints found")
            print("Create some with: python3 flareprox.py create")
            return []

        print(f"\nFlareProx Endpoints ({len(endpoints)} total):")
        print("-" * 80)
        print(f"{'Name':<35} {'URL':<40} {'Status':<8}")
        print("-" * 80)

        for endpoint in endpoints:
            name = endpoint.get("name", "unknown")
            url = endpoint.get("url", "unknown")
            print(f"{name:<35} {url:<40} {'Active':<8}")

        return endpoints


    def test_proxies(self, target_url: str = "https://ifconfig.me/ip", method: str = "GET") -> Dict:
        """Test proxy endpoints and show IP addresses."""
        endpoints = self._load_endpoints()

        if not endpoints:
            print("No proxy endpoints available. Create some first.")
            return {"success": False, "error": "No endpoints available"}

        results = {}
        successful = 0
        unique_ips = set()

        print(f"Testing {len(endpoints)} FlareProx endpoint(s) with {target_url}")

        for endpoint in endpoints:
            name = endpoint.get("name", "unknown")
            print(f"\nTesting endpoint: {name}")

            # Try multiple attempts with different delay
            max_retries = 2
            success = False
            result = None

            for attempt in range(max_retries):
                try:
                    # Add small delay between retries
                    if attempt > 0:
                        time.sleep(1)
                        print(f"   Retry {attempt}...")

                    test_url = f"{endpoint['url']}?url={target_url}"
                    response = requests.request(method, test_url, timeout=30)

                    result = {
                        "success": response.status_code == 200,
                        "status_code": response.status_code,
                        "response_length": len(response.content),
                        "headers": dict(response.headers)
                    }

                    if response.status_code == 200:
                        success = True
                        print(f"Request successful! Status: {result['status_code']}")

                        # Try to extract and show IP address from response
                        try:
                            response_text = response.text.strip()
                            if target_url in ["https://ifconfig.me/ip", "https://httpbin.org/ip"]:
                                if target_url == "https://httpbin.org/ip":
                                    # httpbin returns JSON
                                    data = response.json()
                                    if 'origin' in data:
                                        ip_address = data['origin']
                                        print(f"   Origin IP: {ip_address}")
                                        unique_ips.add(ip_address)
                                else:
                                    # ifconfig.me returns plain text IP
                                    if response_text and len(response_text) < 100:
                                        print(f"   Origin IP: {response_text}")
                                        unique_ips.add(response_text)
                                    else:
                                        print(f"   Response: {response_text[:100]}...")
                            else:
                                print(f"   Response Length: {result['response_length']} bytes")
                        except Exception as e:
                            print(f"   Response Length: {result['response_length']} bytes")

                        successful += 1
                        break  # Success, no need to retry

                    elif response.status_code == 503:
                        print(f"   Server unavailable (503) - target service may be overloaded")
                        if attempt < max_retries - 1:
                            continue  # Retry
                    else:
                        print(f"Request failed! Status: {response.status_code}")
                        break  # Don't retry for other status codes

                except requests.RequestException as e:
                    if attempt < max_retries - 1:
                        print(f"   Connection error, retrying...")
                        continue
                    else:
                        print(f"Request failed: {e}")
                        result = {"success": False, "error": str(e)}
                        break
                except Exception as e:
                    print(f"Test failed: {e}")
                    result = {"success": False, "error": str(e)}
                    break

            results[name] = result if result else {"success": False, "error": "Unknown error"}

        print(f"\nTest Results:")
        print(f"   Working endpoints: {successful}/{len(endpoints)}")
        if successful < len(endpoints):
            failed_count = len(endpoints) - successful
            print(f"   Failed endpoints: {failed_count} (may be due to target service issues)")
        if unique_ips:
            print(f"   Unique IP addresses: {len(unique_ips)}")
            for ip in sorted(unique_ips):
                print(f"      - {ip}")

        return results

    def cleanup_all(self) -> None:
        """Delete all proxy endpoints."""
        if not self.cloudflare:
            raise FlareProxError("FlareProx not configured")

        print(f"\nCleaning up FlareProx endpoints...")

        try:
            self.cloudflare.cleanup_all()
        except FlareProxError as e:
            print(f"Failed to cleanup: {e}")

        # Clear local cache
        if os.path.exists(self.endpoints_file):
            try:
                os.remove(self.endpoints_file)
            except OSError:
                pass


def setup_interactive_config() -> bool:
    """Interactive setup for Cloudflare credentials."""
    print("Getting Cloudflare Credentials:")
    print("1. Sign up at https://cloudflare.com")
    print("2. Go to https://dash.cloudflare.com/profile/api-tokens")
    print("3. Click Create Token and use the 'Edit Cloudflare Workers' template")
    print("4. Set the 'account resources' and 'zone resources' to all. Click 'Continue to Summary'")
    print("5. Click 'Create Token' and copy the token and your Account ID from the dashboard")
    print()

    # Get API token
    api_token = getpass.getpass("Enter your Cloudflare API token: ").strip()
    if not api_token:
        print("API token is required")
        return False

    # Get account ID
    account_id = input("Enter your Cloudflare Account ID: ").strip()
    if not account_id:
        print("Account ID is required")
        return False

    # Create config
    config = {
        "cloudflare": {
            "api_token": api_token,
            "account_id": account_id
        }
    }

    # Save config file (overwrite if exists)
    config_path = "flareprox.json"
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print(f"\nConfiguration saved to {config_path}")
        print("FlareProx is now configured and ready to use!")
        return True
    except IOError as e:
        print(f"Error saving configuration: {e}")
        return False


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(description="FlareProx - Simple URL Redirection via Cloudflare Workers")

    parser.add_argument("command", nargs='?',
                       choices=["create", "list", "test", "cleanup", "help", "config"],
                       help="Command to execute")

    parser.add_argument("--url", help="Target URL")
    parser.add_argument("--method", default="GET", help="HTTP method (default: GET)")
    parser.add_argument("--count", type=int, default=1, help="Number of proxies to create (default: 1)")
    parser.add_argument("--config", help="Configuration file path")

    return parser


def show_help_message() -> None:
    """Display the main help message."""
    print("FlareProx - Simple URL Redirection via Cloudflare Workers")
    print("\nUsage: python3 flareprox.py <command> [options]")
    print("\nCommands:")
    print("  config    Show configuration help and setup")
    print("  create    Create new proxy endpoints")
    print("  list      List all proxy endpoints")
    print("  test      Test proxy endpoints and show IP addresses")
    print("  cleanup   Delete all proxy endpoints")
    print("  help      Show detailed help")
    print("\nExamples:")
    print("  python3 flareprox.py config")
    print("  python3 flareprox.py create --count 2")
    print("  python3 flareprox.py test")
    print("  python3 flareprox.py test --url https://httpbin.org/ip")


def show_config_help() -> None:
    """Display configuration help and interactive setup."""
    print("FlareProx Configuration")
    print("=" * 40)

    # Check if already configured with valid credentials
    config_files = ["flareprox.json", os.path.expanduser("~/.flareprox.json")]
    valid_config_found = False
    existing_config_files = []

    for config_file in config_files:
        if os.path.exists(config_file):
            existing_config_files.append(config_file)
            try:
                with open(config_file, 'r') as f:
                    config_data = json.load(f)
                    cf_config = config_data.get("cloudflare", {})
                    api_token = cf_config.get("api_token", "").strip()
                    account_id = cf_config.get("account_id", "").strip()

                    # Check if we have actual credentials (not empty or placeholder)
                    if (api_token and account_id and
                        api_token not in ["", "your_cloudflare_api_token_here"] and
                        account_id not in ["", "your_cloudflare_account_id_here"] and
                        len(api_token) > 10 and len(account_id) > 10):
                        valid_config_found = True
                        break
            except (json.JSONDecodeError, IOError):
                continue

    if valid_config_found:
        print(f"\nFlareProx is already configured with valid credentials.")
        print("Configuration files found:")
        for config_file in existing_config_files:
            print(f"  - {config_file}")
        print()

        choice = input("Do you want to reconfigure? (y/n): ").lower().strip()
        if choice != 'y':
            return

    elif existing_config_files:
        print(f"\nConfiguration files exist but appear to contain placeholder values:")
        for config_file in existing_config_files:
            print(f"  - {config_file}")
        print()

    print("Setting up FlareProx configuration...")
    print()

    if setup_interactive_config():
        print("\nYou can now use FlareProx:")
        print("  python3 flareprox.py create --count 2")
        print("  python3 flareprox.py test")
    else:
        print("\nConfiguration failed. Please try again.")


def show_detailed_help() -> None:
    """Display detailed help information."""
    print("FlareProx - Detailed Help")
    print("=" * 30)
    print("\nFlareProx provides simple URL redirection through Cloudflare Workers.")
    print("All traffic sent to your FlareProx endpoints will be redirected to")
    print("the target URL you specify, supporting all HTTP methods.")
    print("\nFeatures:")
    print("- Support for all HTTP methods (GET, POST, PUT, DELETE, etc.)")
    print("- Automatic CORS headers")
    print("- IP masking through Cloudflare's global network")
    print("- Simple URL-based redirection")
    print("- Free tier: 100,000 requests/day")


def main():
    """Main entry point."""
    parser = create_argument_parser()
    args = parser.parse_args()

    # Show help if no command provided
    if not args.command:
        show_help_message()
        return

    if args.command == "config":
        show_config_help()
        return

    if args.command == "help":
        show_detailed_help()
        return

    # Initialize FlareProx
    try:
        flareprox = FlareProx(config_file=args.config)
    except Exception as e:
        print(f"Configuration error: {e}")
        return

    if not flareprox.is_configured:
        print("FlareProx not configured. Use 'python3 flareprox.py config' for setup.")
        return

    try:
        if args.command == "create":
            flareprox.create_proxies(args.count)

        elif args.command == "list":
            flareprox.list_proxies()

        elif args.command == "test":
            if args.url:
                flareprox.test_proxies(args.url, args.method)
            else:
                flareprox.test_proxies()  # Use default httpbin.org/ip

        elif args.command == "cleanup":
            confirm = input("Delete ALL FlareProx endpoints? (y/N): ")
            if confirm.lower() == 'y':
                flareprox.cleanup_all()
            else:
                print("Cleanup cancelled.")

    except FlareProxError as e:
        print(f"Error: {e}")
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
    except Exception as e:
        print(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
