import logging
import platform
import subprocess
import psutil
import ipaddress


# Suppresses Scapy no address on IPv4 on MacOS for interfaces not being used.
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)

from django.shortcuts import render, redirect
from .models import ScanResult
from scapy.all import arping, IP, ICMP, sr1
import netifaces
import ipaddress
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
import json
from manuf import manuf
from django.db.models import Q
import socket
import threading
import time
import subprocess
from django.utils import timezone
from ipaddress import ip_address as ip_addr
from django.shortcuts import redirect
from django.contrib import messages

logger = logging.getLogger(__name__)

# Create your views here.


@require_POST
@csrf_exempt
def update_alias(request):
    data = json.loads(request.body)
    device_id = data.get("deviceId")
    new_alias = data.get("alias")

    try:
        device = ScanResult.objects.get(id=device_id)
        device.alias = new_alias
        device.save()
        return JsonResponse({"success": True})
    except ScanResult.DoesNotExist:
        return JsonResponse({"success": False, "error": "Device not found"})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})


def home(request):
    return render(request, "scanner/home.html")


def get_network_interfaces():
    interfaces = []
    for interface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == 2:  # AF_INET (IPv4)
                ip = addr.address
                if not ipaddress.ip_address(ip).is_loopback:
                    name = get_friendly_name(interface)
                    interfaces.append({"name": name, "ip": ip})
    return interfaces


def get_friendly_name(interface):
    if platform.system() == "Windows":
        import winreg

        try:
            reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
            reg_key = winreg.OpenKey(
                reg,
                r"SYSTEM\CurrentControlSet\Control\Network\{4d36e972-e325-11ce-bfc1-08002be10318}",
            )
            sub_key = winreg.OpenKey(reg_key, f"{interface}\\Connection")
            name = winreg.QueryValueEx(sub_key, "Name")[0]
            return name
        except:
            return interface
    else:
        # For macOS and Linux, the interface name is usually already friendly
        return interface


def get_interfaces_fallback():
    interfaces = []
    for iface in netifaces.interfaces():
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET in addrs:
            ip = addrs[netifaces.AF_INET][0]["addr"]
            if not ip.startswith("127."):
                interfaces.append({"name": iface, "ip": ip})
    return interfaces


def scan_network(request):
    interfaces = get_network_interfaces()
    scan_results = []
    p = manuf.MacParser()

    if request.method == "POST":
        selected_interface = request.POST.get("interface")
        logger.debug(f"Selected interface: {selected_interface}")  # Debug print

        try:
            ip_network = ipaddress.ip_network(f"{selected_interface}/24", strict=False)
            ip_range = str(ip_network)
            logger.info(f"IP range to scan: {ip_range}")

            logger.info("Starting ARP scan")
            result = arping(ip_range, verbose=0)[0]
            logger.info(f"ARP scan complete. Found {len(result)} devices")

            for sent, received in result:
                mac = received.hwsrc
                ip = received.psrc
                manufacturer = p.get_manuf_long(mac) or p.get_manuf(mac) or "Unknown"
                scan_result, created = ScanResult.objects.update_or_create(
                    mac_address=mac,
                    defaults={
                        "ip_address": ip,
                        "manufacturer": manufacturer,
                        "last_seen": timezone.now(),
                    },
                )
                scan_results.append(scan_result)

            logger.debug(f"Scan results: {scan_results}")  # Debug print
            logger.info(f"Total scan results: {len(scan_results)}")
        except Exception as e:
            logger.error(f"Error during network scan: {str(e)}")
            scan_results = []

    return render(
        request,
        "scanner/scan_network.html",
        {"interfaces": interfaces, "scan_results": scan_results},
    )


def scan_ports(request):
    # Fetch all IP addresses
    ip_addresses = list(
        ScanResult.objects.values("ip_address", "mac_address", "manufacturer", "alias")
    )

    # Sort the list based on IP address
    ip_addresses.sort(key=lambda x: ip_addr(x["ip_address"]))

    if request.method == "POST":
        ip_address = request.POST.get("ip_address")
        manual_ip = request.POST.get("manual_ip")
        port_range = request.POST.get("port_range", "1-1024")

        if manual_ip:
            ip_address = manual_ip

        start_port, end_port = map(int, port_range.split("-"))

        def port_scan_generator():
            open_ports = {}
            progress = 0

            def grab_banner(sock, port):
                try:
                    if port == 80 or port == 8080:  # HTTP
                        sock.send(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    elif port == 21:  # FTP
                        pass  # FTP servers usually send a banner upon connection
                    elif port == 22:  # SSH
                        pass  # SSH servers usually send a banner upon connection
                    elif port == 25 or port == 587:  # SMTP
                        sock.send(b"EHLO example.com\r\n")
                    else:
                        sock.send(b"\r\n")

                    banner = sock.recv(1024).decode("utf-8", errors="ignore").strip()
                    return banner
                except socket.error:
                    return ""

            def scan_port(port):
                nonlocal progress, ip_address
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2)  # Increased timeout for banner grabbing
                    result = sock.connect_ex((ip_address, port))
                    if result == 0:
                        try:
                            service = socket.getservbyport(port)
                        except OSError:
                            service = "Unknown"

                        banner = grab_banner(sock, port)

                        open_ports[port] = {"service": service, "banner": banner}
                    sock.close()
                except Exception as e:
                    logger.error(f"Error scanning port {port}: {str(e)}")
                finally:
                    progress += 1

            threads = []
            for port in range(start_port, end_port + 1):
                thread = threading.Thread(target=scan_port, args=(port,))
                threads.append(thread)
                thread.start()

            while progress < (end_port - start_port + 1):
                time.sleep(0.1)
                yield f"data: {json.dumps({'progress': progress, 'total': end_port - start_port + 1})}\n\n"

            for thread in threads:
                thread.join()

            scan_result, created = ScanResult.objects.get_or_create(
                ip_address=ip_address
            )
            scan_result.set_open_ports(open_ports)
            scan_result.save()

            yield f"data: {json.dumps({'progress': progress, 'total': end_port - start_port + 1, 'complete': True, 'open_ports': open_ports})}\n\n"

        return StreamingHttpResponse(
            port_scan_generator(), content_type="text/event-stream"
        )

    return render(request, "scanner/scan_ports.html", {"ip_addresses": ip_addresses})


def get_hostname(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except socket.herror:
        return ip  # Return the IP if hostname lookup fails


def scapy_traceroute(ip_address, max_hops=30):
    for ttl in range(1, max_hops + 1):
        start_time = time.time()
        pkt = IP(dst=ip_address, ttl=ttl) / ICMP()
        reply = sr1(pkt, verbose=0, timeout=2)
        end_time = time.time()

        if reply is None:
            yield f"{ttl}\t*\t*\t*"
        else:
            src_ip = reply.src
            hostname = get_hostname(src_ip)
            rtt = (end_time - start_time) * 1000
            yield f"{ttl}\t{src_ip} {hostname}\t{rtt:.2f} ms"

            if reply.type == 0:  # Echo Reply (reached destination)
                break


def traceroute(request):
    if request.method == "POST":
        ip_address = request.POST.get("ip_address")

        def traceroute_generator():
            yield f"data: {json.dumps({'status': 'Starting traceroute...'})}\n\n"

            for line in scapy_traceroute(ip_address):
                yield f"data: {json.dumps({'status': 'In progress', 'line': line})}\n\n"

            yield f"data: {json.dumps({'status': 'Complete'})}\n\n"

        return StreamingHttpResponse(
            traceroute_generator(), content_type="text/event-stream"
        )

    return render(request, "scanner/traceroute.html")


def speedtest(request):
    return render(
        request,
        "scanner/speedtest.html",
        {"speedtest_url": "https://www.speedtest.net/"},
    )


def view_scan_results(request):
    query = request.GET.get("q", "")  # Use an empty string as default
    if query and query != "None":  # Add this check
        scan_results = ScanResult.objects.filter(
            Q(ip_address__icontains=query)
            | Q(mac_address__icontains=query)
            | Q(manufacturer__icontains=query)
            | Q(alias__icontains=query)
        ).order_by("-last_seen")
    else:
        scan_results = ScanResult.objects.all().order_by("-last_seen")

    context = {
        "scan_results": scan_results,
        "query": ""
        if query == "None"
        else query,  # Ensure 'None' is not passed to the template
    }
    return render(request, "scanner/view_scan_results.html", context)


def delete_selected_results(request):
    if request.method == "POST":
        selected_ids = request.POST.getlist("selected_results")
        if selected_ids:
            ScanResult.objects.filter(id__in=selected_ids).delete()
            messages.success(
                request, f"{len(selected_ids)} result(s) deleted successfully."
            )
        else:
            messages.warning(request, "No results were selected for deletion.")
    return redirect("view_scan_results")


def get_ports(request, result_id):
    scan_result = ScanResult.objects.get(id=result_id)
    return JsonResponse({"open_ports": scan_result.get_open_ports()})
