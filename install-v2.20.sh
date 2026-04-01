#!/bin/bash
#
# PVE Disk I/O Monitor v3.0 安装脚本（Web重构版）
# 支持完整安装、仅Web注入、卸载功能
#
# 用法：
#   ./install-v2.20.sh install        # 完整安装（服务+Web）
#   ./install-v2.20.sh install-web    # 仅Web注入（PVE升级后使用）
#   ./install-v2.20.sh uninstall      # 卸载

set -e  # 遇到错误立即退出

################### 配置部分 #############

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# 文件路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/pve-disk-io-monitor"
SCRIPT_INSTALL_PATH="$INSTALL_DIR/pve-disk-io-monitor.py"
ANALYZE_SCRIPT="$INSTALL_DIR/analyze-disk-zfs-log.py"
MODULE_INSTALL_DIR="$INSTALL_DIR/disklog"
SERVICE_NAME="pve-disk-io-monitor"
BACKUP_DIR="$HOME/PVE-DISK-IO-DASHBOARD"

# PVE Web文件
PVE_MANAGER_LIB_JS_FILE="/usr/share/pve-manager/js/pvemanagerlib.js"
NODES_PM_FILE="/usr/share/perl5/PVE/API2/Nodes.pm"

# 日志文件
DISK_LOG_FILE="/var/log/disklog.txt"
ZPOOL_LOG_FILE="/var/log/zpoollog.txt"
OUTPUT_FILE="/run/pve-disk-io/pve-disks-io.log"
TMPFS_DIR="/run/pve-disk-io"

##################### 辅助函数 #######################

msgb() { echo -e "${BOLD}${1}${NC}"; }
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 检查root权限
check_root() {
    if [[ $EUID -ne 0 ]]; then
        err "需要root权限运行此脚本，请使用: sudo $0"
    fi
    info "Root权限验证通过"
}

# 检查Web文件是否存在
check_web_files() {
    [[ -f "$PVE_MANAGER_LIB_JS_FILE" ]] || err "未找到文件: $PVE_MANAGER_LIB_JS_FILE"
    [[ -f "$NODES_PM_FILE" ]] || err "未找到文件: $NODES_PM_FILE"
}

# 检测API注入
is_api_code_injected() {
    grep -q 'my \$disk_zfs_summary_file = "/var/log/disk-zfs-summary.txt";' "$NODES_PM_FILE" 2>/dev/null || \
    grep -q 'my \$disks_io_log_file = "/run/pve-disk-io/pve-disks-io.log";' "$NODES_PM_FILE" 2>/dev/null
}

# 检测UI注入
is_ui_injected() {
    grep -q "itemId: 'disk_zfs_logs'" "$PVE_MANAGER_LIB_JS_FILE" 2>/dev/null || \
    grep -q "itemId: 'disks_io_detail'" "$PVE_MANAGER_LIB_JS_FILE" 2>/dev/null
}

# 检测mod是否已安装
is_mod_installed() {
    grep -q 'disksData' "$NODES_PM_FILE" 2>/dev/null || grep -q 'disksIoHtml' "$NODES_PM_FILE" 2>/dev/null
}

# 重启pveproxy服务
restart_proxy() {
    info "重启PVE代理服务..."
    systemctl restart pveproxy
}

##################### 备份函数 #######################

create_backup_directory() {
    if [[ ! -d "$BACKUP_DIR" ]]; then
        mkdir -p "$BACKUP_DIR" 2>/dev/null || {
            err "无法创建备份目录: $BACKUP_DIR"
        }
        info "已创建备份目录: $BACKUP_DIR"
    else
        info "备份目录已存在: $BACKUP_DIR"
    fi
}

create_file_backup() {
    local source_file="$1"
    local timestamp="$2"
    local filename

    filename=$(basename "$source_file")
    local backup_file="$BACKUP_DIR/disk-io-dashboard.${filename}.$timestamp"

    [[ -f "$source_file" ]] || err "源文件不存在: $source_file"
    [[ -r "$source_file" ]] || err "无法读取源文件: $source_file"

    cp "$source_file" "$backup_file" || err "备份失败: $backup_file"

    # 验证备份完整性
    if ! cmp -s "$source_file" "$backup_file"; then
        err "备份验证失败: $backup_file"
    fi

    info "已创建备份: $backup_file"
}

perform_backup() {
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)

    msgb "\n=== 创建修改文件的备份 ==="

    create_backup_directory
    create_file_backup "$NODES_PM_FILE" "$timestamp"
    create_file_backup "$PVE_MANAGER_LIB_JS_FILE" "$timestamp"
}

##################### Web注入函数 #######################

# 插入API代码到Nodes.pm
insert_node_info() {
    msgb "\n=== 插入磁盘I/O数据获取代码到API ==="

    # 幂等：如果代码块已存在则跳过
    if is_api_code_injected; then
        info "检测到Nodes.pm已包含磁盘I/O代码块，跳过代码注入"
        info "Nodes.pm Web注入已完成，无需重复操作"
        return 0
    fi

    # 首先注入 led_slot 参数定义到 status 端点（GET方法）
    # 这样 PVE API 框架才会接受这个参数
    if grep -B10 "led_slot => {" "$NODES_PM_FILE" | grep -q "Read node status"; then
        info "led_slot 参数定义已存在于 status 端点，跳过"
    else
        msgb "注入 led_slot 参数定义到 status 端点..."

        # 精确匹配：path => 'status' + method => 'GET' + "Read node status"
        # 结构：node => get_standard_option('pve-node'),\n        },
        # 替换为：node => ...,\n            led_slot => ...,\n        },
        perl -i -0777 -pe '
            s/(path\s*=>\s*'\''status'\'',\s*method\s*=>\s*'\''GET'\''.*?description\s*=>\s*"Read node status".*?node\s*=>\s*get_standard_option\('\''pve-node'\''\),)(\s*\},\s*\},)/$1\n\t    led_slot => { type => '\''integer'\'', optional => 1, description => '\''LED slot to toggle'\'' },$2/s;
        ' "$NODES_PM_FILE"

        if grep -B10 "led_slot => {" "$NODES_PM_FILE" | grep -q "Read node status"; then
            info "led_slot 参数定义已注入到 status 端点"
        else
            warn "led_slot 参数定义注入失败，LED功能可能不可用"
        fi
    fi

    # 创建临时文件包含完整Perl代码
    local temp_perl_code="/tmp/disk_io_perl_code.txt"
    cat > "$temp_perl_code" << 'PERL_CODE'
# 磁盘I/O数据收集 - 返回JSON格式数据（v3.0重构版）
use utf8;
use Encode qw(encode decode);
binmode STDOUT, ":utf8";

# LED命令文件路径
my $led_command_file = "/run/pve-disk-io/led_command.json";

# 检查LED命令参数（通过API参数传递）
if (defined $param->{led_slot} && $param->{led_slot} =~ /^(\d+)$/) {
    my $slot = $1;
    my $timestamp = time();
    my $cmd = "{\"slot\": $slot, \"action\": \"toggle\", \"timestamp\": $timestamp}";
    if (open(my $fh, ">", $led_command_file)) {
        print $fh $cmd;
        close($fh);
    }
}

my $disks_io_log_file = "/run/pve-disk-io/pve-disks-io.log";
my $snapraid_log_file = "/var/log/snapraid.log";
my $disk_zfs_summary_file = "/var/log/disk-zfs-summary.txt";
my $disks_io_data = "";
my $snapraid_log_content = "";
my $disk_zfs_log_content = "";

# 读取磁盘I/O数据
if (-f $disks_io_log_file) {
    open(my $fh, "<:encoding(UTF-8)", $disks_io_log_file) or die "无法打开 $disks_io_log_file: $!";
    while (my $line = <$fh>) {
        chomp $line;
        if ($line =~ /^DISKS_IO_DATA=(.*)$/) {
            $disks_io_data = $1;
        }
    }
    close($fh);
}

# 读取磁盘和ZFS日志摘要（最后8行）
if (-f $disk_zfs_summary_file) {
    open(my $fh, "<:encoding(UTF-8)", $disk_zfs_summary_file) or warn "无法打开 $disk_zfs_summary_file: $!";
    my @all_lines = <$fh>;
    close($fh);
    my $max_lines = 8;
    my $start_idx = scalar(@all_lines) > $max_lines ? scalar(@all_lines) - $max_lines : 0;
    for (my $i = $start_idx; $i < scalar(@all_lines); $i++) {
        $disk_zfs_log_content .= $all_lines[$i];
    }
}

# 读取SnapRAID日志
if (-f $snapraid_log_file) {
    open(my $fh, "<:encoding(UTF-8)", $snapraid_log_file) or warn "无法打开 $snapraid_log_file: $!";
    my @all_lines = <$fh>;
    close($fh);
    my $max_lines = 20;
    my $start_idx = scalar(@all_lines) > $max_lines ? scalar(@all_lines) - $max_lines : 0;
    for (my $i = $start_idx; $i < scalar(@all_lines); $i++) {
        $snapraid_log_content .= $all_lines[$i];
    }
}

# 辅助函数：转义JSON字符串
sub escape_json_string {
    my $str = shift // "";
    $str =~ s/\\/\\\\/g;
    $str =~ s/"/\\"/g;
    $str =~ s/\n/\\n/g;
    $str =~ s/\r/\\r/g;
    $str =~ s/\t/\\t/g;
    return $str;
}

# 处理磁盘I/O数据，返回JSON数组
my @disk_json_items;
my $has_slot_info = 0;
my $max_slot = 0;

if ($disks_io_data) {
    my @disk_rows = split(/##ROW##/, $disks_io_data);
    foreach my $row (@disk_rows) {
        if ($row) {
            my @parts = split(/##SPLIT##/, $row, -1);
            if (@parts >= 12) {
                my $slot = $parts[0];
                my $disk_path = $parts[1];
                my $product = $parts[2];
                my $serial_number = $parts[3];
                my $mount_point = $parts[4];
                my $status = $parts[5];
                my $capacity = $parts[6];
                my $read_speed = $parts[7];
                my $write_speed = $parts[8];
                my $io_count = $parts[9];
                my $disk_temp = $parts[10];
                my $led_state = $parts[11];

                # 清理字段
                $slot =~ s/^\s+|\s+$//g;
                $disk_path =~ s/^\s+|\s+$//g;
                $product =~ s/^\s+|\s+$//g;
                $serial_number =~ s/^\s+|\s+$//g;
                $mount_point =~ s/^\s+|\s+$//g;
                $status =~ s/^\s+|\s+$//g;
                $capacity =~ s/^\s+|\s+$//g;
                $capacity =~ s/\s*\([^)]*%\)//g;
                $read_speed =~ s/^\s+|\s+$//g;
                $write_speed =~ s/^\s+|\s+$//g;
                $io_count =~ s/^\s+|\s+$//g;
                $disk_temp =~ s/^\s+|\s+$//g;
                $led_state =~ s/^\s+|\s+$//g;

                # 检测是否有槽位信息
                if ($slot =~ /^\d+$/ && $slot > 0) {
                    $has_slot_info = 1;
                    $max_slot = $slot if $slot > $max_slot;
                }

                # 转义所有字符串字段
                my $e_slot = escape_json_string($slot);
                my $e_disk = escape_json_string($disk_path);
                my $e_product = escape_json_string($product);
                my $e_serial = escape_json_string($serial_number);
                my $e_mount = escape_json_string($mount_point);
                my $e_status = escape_json_string($status);
                my $e_capacity = escape_json_string($capacity);
                my $e_read = escape_json_string($read_speed);
                my $e_write = escape_json_string($write_speed);
                my $e_io = escape_json_string($io_count);
                my $e_temp = escape_json_string($disk_temp);
                my $e_led = escape_json_string($led_state);

                # 构建JSON对象
                my $json_item = "{";
                $json_item .= "\"slot\":\"$e_slot\",";
                $json_item .= "\"disk\":\"$e_disk\",";
                $json_item .= "\"product\":\"$e_product\",";
                $json_item .= "\"serial\":\"$e_serial\",";
                $json_item .= "\"mount\":\"$e_mount\",";
                $json_item .= "\"status\":\"$e_status\",";
                $json_item .= "\"capacity\":\"$e_capacity\",";
                $json_item .= "\"read\":\"$e_read\",";
                $json_item .= "\"write\":\"$e_write\",";
                $json_item .= "\"io\":\"$e_io\",";
                $json_item .= "\"temp\":\"$e_temp\",";
                $json_item .= "\"led\":\"$e_led\"";
                $json_item .= "}";

                push @disk_json_items, $json_item;
            }
        }
    }
}

# 构建完整的磁盘数据JSON（包含配置和磁盘数组）
my $disks_json = "{";
$disks_json .= "\"config\":{";
$disks_json .= "\"hasSlots\":" . ($has_slot_info ? "true" : "false") . ",";
$disks_json .= "\"rows\":3,";
$disks_json .= "\"cols\":3,";  # 3x3布局，槽位映射 1,2,3/5,6,7/9,10,11
$disks_json .= "\"maxSlot\":$max_slot";
$disks_json .= "},";
$disks_json .= "\"disks\":[" . join(",", @disk_json_items) . "]";
$disks_json .= "}";

# 日志内容转义（用于前端显示）
my $e_zfs_log = escape_json_string($disk_zfs_log_content);
my $e_snapraid_log = escape_json_string($snapraid_log_content);

# 将数据传递给API响应
$res->{disksData} = $disks_json;
$res->{diskZfsLog} = $e_zfs_log;
$res->{snapraidLog} = $e_snapraid_log;
PERL_CODE

    # 使用perl一次性注入代码
    perl -i -0777 -pe '
        BEGIN {
            open(my $fh, "<", "/tmp/disk_io_perl_code.txt") or die "Cannot open temp file: $!";
            local $/;
            $::code = <$fh>;
            close($fh);
        }
        s/(my \$dinfo = df\(.*?\);)/$::code\n$1/s;
    ' "$NODES_PM_FILE"

    local exit_code=$?
    rm -f "$temp_perl_code"

    if [[ $exit_code -ne 0 ]]; then
        err "插入磁盘I/O数据获取代码到Nodes.pm失败"
    fi

    # 验证代码块插入
    if ! is_api_code_injected; then
        err "磁盘I/O数据获取代码插入验证失败"
    fi

    info "磁盘I/O数据获取代码已添加到 \"$NODES_PM_FILE\""
}

# 插入UI到pvemanagerlib.js
insert_dashboard_items() {
    msgb "\n=== 插入磁盘笼可视化UI ==="

    # 幂等：UI项已存在则跳过
    if is_ui_injected; then
        info "检测到pvemanagerlib.js已包含磁盘笼UI项，跳过UI注入"
        return 0
    fi

    # 扩展StatusView容器空间（仅在首次安装时执行）
    if ! grep -q "disksData" "$PVE_MANAGER_LIB_JS_FILE" 2>/dev/null; then
        info "扩展StatusView容器空间..."
        sed -i "/Ext.define('PVE\.node\.StatusView'/,/\},/ {
            s/\(bodyPadding:\) '[^']*'/\1 '20 15 20 15'/
            s/height: [0-9]\+/minHeight: 360,\n\tflex: 1,\n\tcollapsible: true,\n\ttitleCollapse: true/
            s/\(tableAttrs:.*$\)/trAttrs: \{ valign: 'top' \},\n\t\1/
        }" "$PVE_MANAGER_LIB_JS_FILE"
        info "StatusView容器空间已扩展"
    fi

    local temp_items_file="/tmp/disk_io_ui_items.js"

    # 使用 trap 确保临时文件清理
    trap "rm -f '$temp_items_file'" EXIT

    # 生成可视化磁盘笼UI项
    cat > "$temp_items_file" << 'ITEMS_EOF'
	{
	    xtype: 'box',
	    colspan: 2,
	    padding: '0 0 20 0',
	},
	{
	    itemId: 'disks_io_detail',
	    colspan: 2,
	    printBar: false,
	    title: gettext('磁盘笼 / Disk Enclosure'),
	    textField: 'disksData',
	    renderer: function(value) {
	        if (!value) return '<div style="padding:10px;text-align:center;">未找到磁盘数据</div>';
	        try {
	            var data = JSON.parse(value);
	            if (!data) return '<div style="padding:10px;text-align:center;">无磁盘信息</div>';
	            var disks = data.disks || [];
	            if (disks.length === 0) return '<div style="padding:10px;text-align:center;">无磁盘信息</div>';
	            var slotMap = [1,2,3,5,6,7,9,10,11];  // 3.5寸槽位
	            var nvmeSlotMap = [1,3,5,0,2,4];  // 2.5寸NVMe槽位（上排1,3,5 下排0,2,4）
	            var diskMap = {};      // 3.5寸槽位设备
	            var nvmeDiskMap = {};  // 2.5寸NVMe槽位设备
	            var noSlotDisks = [];
	            for (var i = 0; i < disks.length; i++) {
	                var d = disks[i];
	                if (d.slot && d.slot !== '') {
	                    if (d.slot.startsWith('nvme_')) {
	                        // NVMe槽位，格式为 nvme_2, nvme_3 等
	                        nvmeDiskMap[d.slot.substring(5)] = d;
	                    } else {
	                        diskMap[d.slot] = d;
	                    }
	                } else {
	                    noSlotDisks.push(d);
	                }
	            }
	            // 温度颜色函数: <=54正常(inherit), 55-59橙色警告, >=60红色危险
	            var getTempColor = function(tempStr) {
	                if (!tempStr) return 'inherit';
	                var num = parseInt(tempStr.replace(/[^0-9]/g, ''));
	                if (isNaN(num)) return 'inherit';
	                if (num >= 60) return '#d9534f';
	                if (num >= 55) return '#f0ad4e';
	                return 'inherit';
	            };
	            var html = '<div class="disk-cage-container" style="padding:10px;">';
	            html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">';
	            for (var i = 0; i < 9; i++) {
	                var slot = slotMap[i];
	                var disk = diskMap[slot];
	                var isEmpty = !disk;
	                var isOffline = disk && disk.status === '离线';
	                var ledColor = 'currentColor';
	                var ledShadow = 'none';
	                var ledOpacity = '0.3';
	                if (disk && disk.led === '1') { ledColor = '#3e8ed0'; ledShadow = '0 0 6px #3e8ed0'; ledOpacity = '1'; }
	                var borderStyle = (isEmpty || isOffline) ? 'dashed' : 'solid';
	                var cardOpacity = (isEmpty || isOffline) ? '0.5' : '1';
	                html += '<div class="disk-slot" data-slot="' + slot + '" style="border:1px ' + borderStyle + ';border-radius:3px;padding:10px;cursor:pointer;opacity:' + cardOpacity + ';display:flex;flex-direction:column;">';
	                // 上半部分：前两行，占据整个宽度
	                html += '<div style="width:100%;font-size:12px;line-height:1.5;">';
	                if (!isEmpty) {
	                    var sn = disk.serial || '';
	                    var mount = disk.mount || '-';
	                    var dev = disk.disk || '';
	                    if (dev.startsWith('/dev/')) dev = dev.substring(5);
	                    var product = disk.product || '-';
	                    var cap = disk.capacity || '-';
	                    var r = disk.read || '0';
	                    var w = disk.write || '0';
	                    var io = disk.io || '0';
	                    var temp = (disk.temp && disk.temp !== 'N/A' && disk.temp !== '-') ? disk.temp : '';
	                    var tempColor = getTempColor(temp);
	                    
	                    // 处理 ZFS 池显示格式：ZFS:zfs_12t 10.9T/2.81T(DEGRADED) -> ⚙ zfs_12t 10.9T/2.81T ⚠
	                    var mountDisplay = mount;
	                    if (mount.startsWith('ZFS:')) {
	                        var zfsInfo = mount.substring(4); // 去掉 "ZFS:"
	                        var match = zfsInfo.match(/^(\S+)\s+([\d.TGM\/]+)\((\w+)\)$/);
	                        if (match) {
	                            var poolName = match[1];
	                            var poolSize = match[2];
	                            var health = match[3];
	                            var statusIcon = '✓';
	                            var statusColor = '#5cb85c';
	                            if (health === 'DEGRADED') {
	                                statusIcon = '⚠';
	                                statusColor = '#f0ad4e';
	                            } else if (health === 'OFFLINE' || health === 'FAULTED' || health === 'UNAVAIL') {
	                                statusIcon = '✗';
	                                statusColor = '#d9534f';
	                            }
	                            mountDisplay = '⚙ ' + poolName + ' ' + poolSize + ' <span style="color:' + statusColor + ';">' + statusIcon + '</span>';
	                        }
	                    }
	                    // 行1: LED + 型号 + 容量
	                    html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
	                    html += '<div style="display:flex;align-items:center;">';
	                    html += '<span class="led-btn" data-slot="' + slot + '" data-led="' + (disk.led || '0') + '" style="display:inline-block;width:10px;height:10px;background:' + ledColor + ';opacity:' + ledOpacity + ';border-radius:50%;box-shadow:' + ledShadow + ';margin-right:6px;cursor:pointer;" title="点击切换LED"></span>';
	                    html += '<span>' + product + '</span>';
	                    html += '</div>';
	                    html += '<span style="margin-left:auto;">' + cap + '</span>';
	                    html += '</div>';
                    // 行2: 挂载点
                    html += '<div style="display:flex;justify-content:space-between;">';
                    html += '<span>' + mountDisplay + '</span>';
                    html += '</div>';
	                }
	                html += '</div>';
	                // 下半部分：槽位号 + 后两行
	                html += '<div style="display:flex;font-size:12px;line-height:1.5;">';
	                // 槽位号：16px，靠下对齐
	                html += '<div style="font-size:16px;font-weight:bold;min-width:20px;display:flex;align-items:flex-end;justify-content:center;padding-bottom:4px;margin-right:4px;opacity:' + (isEmpty ? '0.5' : '1') + ';">' + slot + '</div>';
	                html += '<div style="flex:1;overflow:hidden;">';
	                if (isEmpty) {
	                    html += '<div style="text-align:center;opacity:0.5;">Empty</div>';
	                } else {
	                    var sn = disk.serial || '';
	                    var r = disk.read || '0';
	                    var w = disk.write || '0';
	                    var io = disk.io || '0';
	                    var temp = (disk.temp && disk.temp !== 'N/A' && disk.temp !== '-') ? disk.temp : '';
	                    var tempColor = getTempColor(temp);
                    var ioColor = disk.status === '活跃' ? '#5cb85c' : 'inherit';
                    // 行3: SN（后8位）+ 设备名
                    html += '<div style="display:flex;justify-content:space-between;">';
                    html += '<span>SN:' + sn.substring(sn.length - 8) + '</span>';
                    html += '<span>' + dev + '</span>';
                    html += '</div>';
                    // 行4: 读写速度 + IO + 温度
                    html += '<div style="display:flex;justify-content:space-between;">';
                    html += '<span>R:' + r + ' W:' + w + '</span>';
                    html += '<span style="color:' + ioColor + ';">IO:' + io + '</span>';
                    if (temp) html += '<span style="color:' + tempColor + ';">' + temp + '</span>';
                    html += '</div>';
	                }
	                html += '</div>';
	                html += '</div>';
	                html += '</div>';
	            }
	            html += '</div>';
	            // 2.5寸NVMe槽位区域
	            html += '<div style="margin-top:12px;border-top:1px solid;padding-top:10px;">';
	            html += '<div style="font-size:12px;margin-bottom:8px;">2.5寸槽位 (NVMe)</div>';
	            html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">';
	            for (var i = 0; i < 6; i++) {
	                var slot = nvmeSlotMap[i];
	                var disk = nvmeDiskMap[slot];
	                var isEmpty = !disk;
	                var isOffline = disk && disk.status === '离线';
	                var borderStyle = (isEmpty || isOffline) ? 'dashed' : 'solid';
	                var cardOpacity = (isEmpty || isOffline) ? '0.5' : '1';
	                html += '<div class="nvme-slot" data-slot="nvme_' + slot + '" style="border:1px ' + borderStyle + ';border-radius:3px;padding:8px;opacity:' + cardOpacity + ';display:flex;flex-direction:column;font-size:11px;line-height:1.5;">';
	                if (isEmpty) {
	                    html += '<div style="display:flex;">';
	                    html += '<div style="font-size:14px;font-weight:bold;min-width:18px;display:flex;align-items:flex-end;justify-content:center;padding-bottom:4px;margin-right:4px;opacity:0.5;">' + slot + '</div>';
	                    html += '<div style="flex:1;display:flex;align-items:center;justify-content:center;opacity:0.5;">Empty</div>';
	                    html += '</div>';
	                } else {
	                    var product = disk.product || '-';
	                    var cap = disk.capacity || '-';
	                    var mount = disk.mount || '-';
	                    var sn = disk.serial || '';
	                    var dev = disk.disk || '';
	                    if (dev.startsWith('/dev/')) dev = dev.substring(5);
	                    var r = disk.read || '0';
	                    var w = disk.write || '0';
	                    var io = disk.io || '0';
	                    var temp = (disk.temp && disk.temp !== 'N/A' && disk.temp !== '-') ? disk.temp : '';
	                    var tempColor = getTempColor(temp);
	                    var ioColor = disk.status === '活跃' ? '#5cb85c' : 'inherit';
	                    
	                    // 处理 ZFS 池显示格式：ZFS:rpool 3.48T/882G(ONLINE) -> ⚙ rpool 3.48T/882G ✓
	                    var mountDisplay = mount;
	                    if (mount.startsWith('ZFS:')) {
	                        var zfsInfo = mount.substring(4); // 去掉 "ZFS:"
	                        var match = zfsInfo.match(/^(\S+)\s+([\d.TGM\/]+)\((\w+)\)$/);
	                        if (match) {
	                            var poolName = match[1];
	                            var poolSize = match[2];
	                            var health = match[3];
	                            var statusIcon = '✓';
	                            var statusColor = '#5cb85c';
	                            if (health === 'DEGRADED') {
	                                statusIcon = '⚠';
	                                statusColor = '#f0ad4e';
	                            } else if (health === 'OFFLINE' || health === 'FAULTED' || health === 'UNAVAIL') {
	                                statusIcon = '✗';
	                                statusColor = '#d9534f';
	                            }
	                            mountDisplay = '⚙ ' + poolName + ' ' + poolSize + ' <span style="color:' + statusColor + ';">' + statusIcon + '</span>';
	                        }
	                    }
	                    
	                    // 上半部分：前两行
	                    html += '<div style="width:100%;">';
	                    // 行1: 型号 + 容量
	                    html += '<div style="display:flex;justify-content:space-between;">';
	                    html += '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:65%;">' + product + '</span>';
	                    html += '<span>' + cap + '</span>';
	                    html += '</div>';
	                    // 行2: 挂载点
	                    html += '<div style="display:flex;justify-content:space-between;">';
	                    html += '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + mountDisplay + '</span>';
	                    html += '</div>';
	                    html += '</div>';
	                    
	                    // 下半部分：槽位号 + 后两行
	                    html += '<div style="display:flex;">';
	                    // 槽位号：14px，靠下对齐
	                    html += '<div style="font-size:14px;font-weight:bold;min-width:18px;display:flex;align-items:flex-end;justify-content:center;padding-bottom:4px;margin-right:4px;">' + slot + '</div>';
	                    html += '<div style="flex:1;">';
	                    // 行3: SN（后8位）+ 设备名
	                    html += '<div style="display:flex;justify-content:space-between;">';
	                    html += '<span>SN:' + sn.substring(sn.length - 8) + '</span>';
	                    html += '<span>' + dev + '</span>';
	                    html += '</div>';
	                    // 行4: 读写速度 + IO + 温度
	                    html += '<div style="display:flex;justify-content:space-between;">';
	                    html += '<span>R:' + r + ' W:' + w + '</span>';
	                    html += '<span style="color:' + ioColor + ';">IO:' + io + '</span>';
	                    if (temp) html += '<span style="color:' + tempColor + ';">' + temp + '</span>';
	                    html += '</div>';
	                    html += '</div>';
	                    html += '</div>';
	                }
	                html += '</div>';
	            }
	            html += '</div>';
	            html += '</div>';
	            if (noSlotDisks.length > 0) {
	                html += '<div style="margin-top:12px;border-top:1px solid;padding-top:10px;">';
	                html += '<div style="font-size:12px;margin-bottom:8px;">其他磁盘（无槽位）</div>';
	                html += '<div style="display:flex;flex-direction:column;gap:6px;">';
	                for (var i = 0; i < noSlotDisks.length; i++) {
	                    var disk = noSlotDisks[i];
	                    var dev = disk.disk || '';
	                    if (dev.startsWith('/dev/')) dev = dev.substring(5);
	                    var mount = disk.mount || '-';
	                    var product = disk.product || '-';
	                    var cap = disk.capacity || '-';
	                    var r = disk.read || '0';
	                    var w = disk.write || '0';
	                    var io = disk.io || '0';
	                    var temp = (disk.temp && disk.temp !== 'N/A' && disk.temp !== '-') ? disk.temp : '';
	                    var tempColor = getTempColor(temp);
	                    var ioColor = disk.status === '活跃' ? '#5cb85c' : 'inherit';
	                    html += '<div style="border:1px solid;border-radius:3px;padding:8px 12px;display:flex;align-items:center;font-size:12px;gap:8px;">';
	                    html += '<span style="font-weight:bold;">' + dev + '</span>';
	                    html += '<span style="flex:1;">' + product + '</span>';
	                    html += '<span>' + mount + '</span>';
	                    html += '<span>R:' + r + ' W:' + w + '</span>';
	                    html += '<span style="color:' + ioColor + ';">io:' + io + '</span>';
	                    html += '<span>' + cap + '</span>';
	                    if (temp) html += '<span style="color:' + tempColor + ';">' + temp + '</span>';
	                    html += '</div>';
	                }
	                html += '</div>';
	                html += '</div>';
	            }
	            html += '</div>';
	            return html;
	        } catch(e) {
	            console.error('磁盘数据解析错误:', e);
	            return '<div style="padding:10px;color:#d9534f;">数据解析错误: ' + e.message + '</div>';
	        }
	    }
	},
	{
	    itemId: 'disk_zfs_logs',
	    colspan: 2,
	    printBar: false,
	    title: gettext('磁盘和ZFS日志摘要'),
	    textField: 'diskZfsLog',
	    renderer: function(value) {
	        if (!value || value === '') return '<div style="padding:10px;text-align:center;">未找到日志摘要</div>';
	        // 将转义的\n转换为真正的换行符
	        var formattedValue = value.replace(/\\n/g, '\n').replace(/\\r/g, '');
	        return '<div style="max-height:200px;overflow-y:auto;border:1px solid;border-radius:3px;"><pre style="font-family:monospace;font-size:12px;white-space:pre-wrap;padding:10px;margin:0;">' + formattedValue + '</pre></div>';
	    }
	},
	{
	    itemId: 'snapraid_logs',
	    colspan: 2,
	    printBar: false,
	    title: gettext('SnapRAID 最近日志'),
	    textField: 'snapraidLog',
	    renderer: function(value) {
	        if (!value || value === '') return '<div style="padding:10px;text-align:center;">未找到SnapRAID日志</div>';
	        // 将转义的\n转换为真正的换行符
	        var formattedValue = value.replace(/\\n/g, '\n').replace(/\\r/g, '');
	        return '<div style="max-height:200px;overflow-y:auto;border:1px solid;border-radius:3px;"><pre style="font-family:monospace;font-size:12px;white-space:pre-wrap;padding:10px;margin:0;">' + formattedValue + '</pre></div>';
	    }
	},
ITEMS_EOF

    # 使用perl一次性插入UI项（插入到cpus项之前）
    perl -i -0777 -pe '
        BEGIN {
            open(my $fh, "<", "/tmp/disk_io_ui_items.js") or die "Cannot open items file: $!";
            local $/;
            $::items = <$fh>;
            close($fh);
        }
        # 查找cpus项并在其之前插入磁盘笼UI项
        s/(\{\s*xtype:\s*'\''box'\'',\s*colspan:\s*2,\s*padding:\s*'\''0 0 20 0'\'',\s*\},[\s\n]*)(\{\s*itemId:\s*'\''cpus'\'')/$1$::items$2/s;
    ' "$PVE_MANAGER_LIB_JS_FILE"

    local insert_status=$?

    if [[ $insert_status -ne 0 ]]; then
        err "插入磁盘笼UI项到pvemanagerlib.js失败"
    fi

    # 验证插入
    if ! grep -q "itemId: 'disks_io_detail'" "$PVE_MANAGER_LIB_JS_FILE"; then
        err "磁盘笼UI项插入验证失败"
    fi
    if ! grep -q "itemId: 'disk_zfs_logs'" "$PVE_MANAGER_LIB_JS_FILE"; then
        err "磁盘和ZFS日志摘要 UI项插入验证失败"
    fi

    # 清理临时文件并移除 trap
    rm -f "$temp_items_file"
    trap - EXIT

    info "磁盘笼可视化UI已插入到 \"$PVE_MANAGER_LIB_JS_FILE\""
}

# 插入LED点击事件处理代码
insert_led_event_handler() {
    msgb "\n=== 插入LED事件处理代码 ==="

    # 检查是否已注入
    if grep -q "pve-disk-led-handler" "$PVE_MANAGER_LIB_JS_FILE" 2>/dev/null; then
        info "LED事件处理代码已存在，跳过"
        return 0
    fi

    local temp_handler_file="/tmp/led_event_handler.js"

    cat > "$temp_handler_file" << 'HANDLER_EOF'
// PVE Disk LED Handler (pve-disk-led-handler)
(function() {
    // 使用事件委托绑定LED点击事件
    Ext.onReady(function() {
        Ext.getBody().on('click', function(e, target) {
            var el = Ext.get(target);
            if (el && el.hasCls('led-btn')) {
                var slot = el.getAttribute('data-slot');
                if (slot) {
                    // 发送LED切换请求
                    Ext.Ajax.request({
                        url: '/api2/json/nodes/' + Proxmox.NodeName + '/status',
                        method: 'GET',
                        params: { led_slot: slot },
                        success: function(response) {
                            console.log('LED toggle sent for slot ' + slot);
                            // 切换LED显示状态
                            var currentLed = el.getAttribute('data-led');
                            var newLed = (currentLed === '1') ? '0' : '1';
                            el.setAttribute('data-led', newLed);
                            if (newLed === '1') {
                                el.setStyle({background: '#3e8ed0', boxShadow: '0 0 6px #3e8ed0', opacity: '1'});
                            } else {
                                el.setStyle({background: 'currentColor', boxShadow: 'none', opacity: '0.3'});
                            }
                        },
                        failure: function() {
                            console.log('LED toggle request completed');
                        }
                    });
                }
            }
        }, null, {delegate: '.led-btn'});
    });
})();

HANDLER_EOF

    # 在文件末尾插入事件处理代码
    cat "$temp_handler_file" >> "$PVE_MANAGER_LIB_JS_FILE"

    rm -f "$temp_handler_file"

    # 验证插入
    if ! grep -q "pve-disk-led-handler" "$PVE_MANAGER_LIB_JS_FILE"; then
        err "LED事件处理代码插入验证失败"
    fi

    info "LED事件处理代码已插入"
}

##################### 服务安装函数 #######################

install_service() {
    msgb "\n=== 安装磁盘I/O监控服务 ==="

    # 检查依赖
    info "检查系统依赖..."

    if ! command -v python3 &> /dev/null; then
        err "未找到 python3，请先安装: apt install python3"
    fi

    if ! command -v smartctl &> /dev/null; then
        warn "未找到 smartctl，安装中..."
        apt-get update && apt-get install -y smartmontools || err "安装 smartmontools 失败"
    fi

    # sg3-utils 用于插槽LED控制
    if ! command -v sg_ses &> /dev/null; then
        warn "未找到 sg_ses（插槽LED控制需要），安装中..."
        apt-get update && apt-get install -y sg3-utils || warn "安装 sg3-utils 失败，插槽功能可能不可用"
    fi

    if ! command -v lsblk &> /dev/null; then
        err "未找到 lsblk，请先安装: apt install util-linux"
    fi

    info "所有依赖已满足"

    # 检查文件
    info "检查安装文件..."

    if [[ ! -d "$SCRIPT_DIR/disklog" ]]; then
        err "未找到 disklog 模块目录"
    fi

    if [[ ! -f "$SCRIPT_DIR/pve-disk-io-monitor-v2.20.py" ]]; then
        err "未找到主程序文件: pve-disk-io-monitor-v2.20.py"
    fi

    # 停止旧服务
    info "停止旧版本服务..."
    if systemctl is-active --quiet $SERVICE_NAME; then
        systemctl stop $SERVICE_NAME
    fi

    # 手动停止可能的孤立进程
    if pgrep -f "pve-disk-io-monitor" > /dev/null; then
        pkill -f "pve-disk-io-monitor" || true
        sleep 2
    fi

    # 设置内存文件系统
    info "设置内存文件系统..."
    mkdir -p "$TMPFS_DIR"
    if ! mountpoint -q "$TMPFS_DIR"; then
        mount -t tmpfs -o size=10M,mode=0755 tmpfs "$TMPFS_DIR"
        info "已挂载tmpfs到 $TMPFS_DIR"
    else
        info "$TMPFS_DIR 已经挂载为tmpfs"
    fi

    # 安装文件
    info "安装程序文件..."

    mkdir -p "$INSTALL_DIR"
    cp -r "$SCRIPT_DIR/disklog" "$INSTALL_DIR/"
    chmod -R 755 "$INSTALL_DIR/disklog"

    cp "$SCRIPT_DIR/pve-disk-io-monitor-v2.20.py" "$SCRIPT_INSTALL_PATH"
    chmod +x "$SCRIPT_INSTALL_PATH"

    # 安装分析脚本（如果存在）
    if [[ -f "$SCRIPT_DIR/analyze-disk-zfs-log.py" ]]; then
        cp "$SCRIPT_DIR/analyze-disk-zfs-log.py" "$ANALYZE_SCRIPT"
        chmod +x "$ANALYZE_SCRIPT"
        info "分析脚本已安装到: $ANALYZE_SCRIPT"
    fi

    info "程序文件已安装到: $INSTALL_DIR"

    # 测试导入
    info "测试模块导入..."
    if python3 -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from disklog.config import Config; Config().validate(); print('OK')" 2>&1 | grep -q "OK"; then
        info "模块导入测试通过"
    else
        err "模块导入失败"
    fi

    # 配置systemd服务
    info "配置systemd服务..."

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PVE Disk I/O Monitor v2.20
After=network.target

[Service]
Type=simple
ExecStartPre=/bin/mkdir -p $TMPFS_DIR
ExecStartPre=/bin/mount -t tmpfs -o size=10M,mode=0755 tmpfs $TMPFS_DIR
ExecStart=/usr/bin/python3 $SCRIPT_INSTALL_PATH
ExecStopPost=/bin/umount -l $TMPFS_DIR || true
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
WorkingDirectory=/tmp
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable $SERVICE_NAME

    # 配置drivetemp模块
    info "配置drivetemp模块..."
    if ! lsmod | grep -q drivetemp; then
        if modprobe drivetemp 2>/dev/null; then
            info "已加载 drivetemp 模块"
            if ! grep -q "^drivetemp$" /etc/modules 2>/dev/null; then
                echo "drivetemp" >> /etc/modules
                info "已配置开机自动加载"
            fi
        else
            warn "无法加载 drivetemp（内核可能 < 5.6），将使用smartctl降级"
        fi
    else
        info "drivetemp 已加载"
    fi

    # 创建数据目录和默认配置文件
    info "初始化配置文件..."
    mkdir -p /var/lib/disklog

    # 创建默认 nvme_slot_mapping.json（如果不存在）
    if [[ ! -f "/var/lib/disklog/nvme_slot_mapping.json" ]]; then
        cat > /var/lib/disklog/nvme_slot_mapping.json << 'EOF'
{
  "vendor_device": "10b5:9733",
  "detect_port": "01",
  "slot_map": {
    "04": "2",
    "05": "3",
    "06": "4",
    "07": "5"
  }
}
EOF
        info "已创建默认 nvme_slot_mapping.json"
    else
        info "nvme_slot_mapping.json 已存在"
    fi

    # 初始化日志文件
    info "初始化日志文件..."

    if [[ ! -f "$DISK_LOG_FILE" ]]; then
        TIMESTAMP=$(date '+%Y/%m/%d %H:%M:%S')

        LSBLK_SIMPLE=$(lsblk -o NAME,SIZE,TYPE -P -n 2>/dev/null | \
            grep -v '^NAME="loop\|^NAME="ram\|^NAME="dm-\|^NAME="zram\|^NAME="zd' | \
            grep 'TYPE="disk"\|TYPE="rom"' || echo "(无物理设备)")

        LSBLK_DETAILED=$(lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINT,FSTYPE,WWN -P -n 2>/dev/null | \
            grep -v '^NAME="loop\|^NAME="ram\|^NAME="dm-\|^NAME="zram\|^NAME="zd' | \
            grep 'TYPE="disk"' || echo "(无物理设备)")

        cat > "$DISK_LOG_FILE" <<EOF
=== lsblk 历史（最近1次）===

--- 历史记录 [$TIMESTAMP] ---
# 获取命令：lsblk -o NAME,SIZE,TYPE -P -n
$LSBLK_SIMPLE

==================================================

[$TIMESTAMP] 系统初始化
--- lsblk 输出 ---
# 获取命令：lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINT,FSTYPE,WWN -P -n
$LSBLK_DETAILED

EOF
        info "已创建 $DISK_LOG_FILE"
    else
        info "disklog.txt 已存在"
    fi

    if [[ ! -f "$ZPOOL_LOG_FILE" ]]; then
        TIMESTAMP=$(date '+%Y/%m/%d/%H:%M')

        ZPOOL_LIST=$(zpool list -H -o name,size,allocated,health 2>/dev/null || echo "")

        cat > "$ZPOOL_LOG_FILE" <<EOF
=== zpool list 历史（最近1次）===

--- 历史记录 [$TIMESTAMP] ---
# 获取命令：zpool list -H -o name,size,allocated,health
$ZPOOL_LIST

==================================================

[$TIMESTAMP] 系统初始化
--- zpool status ---
$(zpool status 2>/dev/null || echo "无ZFS池")

EOF
        info "已初始化 $ZPOOL_LOG_FILE"
    else
        info "zpoollog.txt 已存在"
    fi

    # 启动服务
    info "启动服务..."
    systemctl start $SERVICE_NAME
    sleep 3

    if systemctl is-active --quiet $SERVICE_NAME; then
        info "服务启动成功"
    else
        err "服务启动失败，请检查: journalctl -u $SERVICE_NAME -n 50"
    fi

    # 验证功能
    info "验证功能..."
    if [[ -f "$OUTPUT_FILE" ]]; then
        info "输出文件已生成: $OUTPUT_FILE"

        if grep -q "##SPLIT##" "$OUTPUT_FILE" && grep -q "##ROW##" "$OUTPUT_FILE"; then
            info "输出格式验证通过"
        else
            warn "输出格式可能不正确"
        fi
    else
        warn "输出文件尚未生成，请等待1-2秒"
    fi

    info "磁盘I/O监控服务已安装成功"
}

##################### 主命令函数 #######################

# 完整安装（服务+Web）
cmd_install() {
    msgb "\n========================================"
    msgb "PVE Disk I/O Monitor v2.20 完整安装"
    msgb "========================================"

    check_root

    # 检查Web文件
    check_web_files

    # 检查是否已安装Web注入
    if is_mod_installed; then
        err "Web注入已安装，如需重新安装请先卸载: $0 uninstall"
    fi

    # 备份Web文件
    perform_backup

    # 安装服务
    install_service

    # Web注入
    insert_node_info
    insert_dashboard_items
    insert_led_event_handler

    # 重启代理
    restart_proxy

    msgb "\n========================================"
    msgb "✓ 安装完成！"
    msgb "========================================"
    echo
    echo "重要：请清除浏览器缓存（Ctrl+Shift+R）以查看Web界面更改"
    echo
    echo "程序目录: $INSTALL_DIR"
    echo
    echo "新功能："
    echo "  - 可视化磁盘笼界面（有槽位信息时显示3×3网格）"
    echo "  - LED点击控制（点击LED图标切换状态）"
    echo
    echo "快速命令:"
    echo "  查看状态:   systemctl status $SERVICE_NAME"
    echo "  查看日志:   journalctl -u $SERVICE_NAME -f"
    echo "  输出文件:   cat $OUTPUT_FILE"
    echo "  事件日志:   tail -f /var/log/disklog.txt"
    echo "  分析脚本:   $ANALYZE_SCRIPT"
    echo
}

# 仅Web注入（PVE升级后使用）
cmd_install_web() {
    msgb "\n========================================"
    msgb "仅Web注入（不重装服务）"
    msgb "========================================"

    check_root
    check_web_files

    # 备份Web文件
    perform_backup

    # Web注入
    insert_node_info
    insert_dashboard_items
    insert_led_event_handler

    # 重启代理
    restart_proxy

    msgb "\n========================================"
    msgb "✓ Web注入完成！"
    msgb "========================================"
    echo
    echo "重要：请清除浏览器缓存（Ctrl+Shift+R）以查看更改"
    echo
    echo "新功能："
    echo "  - 可视化磁盘笼界面（有槽位信息时显示3×3网格）"
    echo "  - LED点击控制"
    echo
    echo "提示：此模式不会安装/重启监控服务，仅用于修复升级覆盖的Web文件"
    echo
}

# 卸载
cmd_uninstall() {
    msgb "\n========================================"
    msgb "卸载磁盘I/O监控Mod"
    msgb "========================================"

    check_root

    # 检查是否已安装
    if ! is_mod_installed; then
        err "磁盘I/O监控mod未安装"
    fi

    # 检查其他mod冲突
    local other_mods_detected=false
    local detected_mods=""

    if grep -q 'gpuDriverVersion' "$NODES_PM_FILE" 2>/dev/null; then
        other_mods_detected=true
        detected_mods="${detected_mods}pve-gpu-dashboard "
    fi

    if grep -q 'sensorsOutput' "$NODES_PM_FILE" 2>/dev/null; then
        other_mods_detected=true
        detected_mods="${detected_mods}pve-mod-gui-sensors "
    fi

    if [[ "$other_mods_detected" == true ]]; then
        warn "检测到其他PVE mod: $detected_mods"
        warn "从备份恢复将删除在此磁盘I/O监控备份之后安装的所有mod"
        msgb "\n您有两个选择:"
        echo "  1) 继续 - 恢复备份，然后重新安装其他mod"
        echo "  2) 取消 - 手动删除磁盘I/O监控代码"
        echo ""
        read -p "继续恢复备份？(y/N): " confirm
        if [[ ! "$confirm" =~ ^[yY]$ ]]; then
            info "卸载已取消"
            msgb "\n要手动删除，请编辑以下文件:"
            echo "  - $NODES_PM_FILE (删除disksIoHtml相关行)"
            echo "  - $PVE_MANAGER_LIB_JS_FILE (删除磁盘I/O UI项)"
            exit 0
        fi
    fi

    # 停止并禁用服务
    if systemctl is-active --quiet $SERVICE_NAME; then
        info "停止并禁用磁盘I/O监控服务..."
        systemctl stop $SERVICE_NAME
        systemctl disable $SERVICE_NAME
    fi

    # 卸载tmpfs
    if mountpoint -q "$TMPFS_DIR"; then
        info "卸载tmpfs..."
        umount -l "$TMPFS_DIR" || true
    fi

    # 删除临时目录
    [[ -d "$TMPFS_DIR" ]] && rmdir "$TMPFS_DIR" 2>/dev/null || true

    # 删除服务文件
    if [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
        info "删除服务文件..."
        rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
        systemctl daemon-reload
    fi

    # 删除程序文件
    [[ -d "$INSTALL_DIR" ]] && rm -rf "$INSTALL_DIR"
    info "已删除程序目录: $INSTALL_DIR"

    # 删除输出文件
    [[ -f "$OUTPUT_FILE" ]] && rm -f "$OUTPUT_FILE"

    info "恢复修改的文件..."

    # 查找最新备份文件的通用函数
    find_latest_backup() {
        local filename_pattern="$1"
        find "$BACKUP_DIR" -name "$filename_pattern" -type f -printf '%T+ %p\n' 2>/dev/null | sort -r | head -n 1 | awk '{print $2}'
    }

    # 查找最新的Nodes.pm备份
    local latest_nodes_pm
    latest_nodes_pm=$(find_latest_backup "disk-io-dashboard.Nodes.pm.*")

    if [[ -n "$latest_nodes_pm" ]]; then
        msgb "从备份恢复Nodes.pm: $latest_nodes_pm"
        cp "$latest_nodes_pm" "$NODES_PM_FILE"
        info "Nodes.pm恢复成功"
    else
        warn "未找到Nodes.pm备份"
        warn "您可以重新安装pve-manager包来恢复: apt install --reinstall pve-manager"
    fi

    # 查找最新的pvemanagerlib.js备份
    local latest_pvemanagerlibjs
    latest_pvemanagerlibjs=$(find_latest_backup "disk-io-dashboard.pvemanagerlib.js.*")

    if [[ -n "$latest_pvemanagerlibjs" ]]; then
        msgb "从备份恢复pvemanagerlib.js: $latest_pvemanagerlibjs"
        cp "$latest_pvemanagerlibjs" "$PVE_MANAGER_LIB_JS_FILE"
        info "pvemanagerlib.js恢复成功"
    else
        warn "未找到pvemanagerlib.js备份"
        warn "您可以重新安装pve-manager包来恢复: apt install --reinstall pve-manager"
    fi

    restart_proxy

    msgb "\n========================================"
    msgb "✓ 卸载完成！"
    msgb "========================================"
    echo
    echo "重要：请清除浏览器缓存（Ctrl+Shift+R）以查看更改"
    echo
}

# 显示帮助
show_usage() {
    msgb "\n用法: $0 [install | install-web | uninstall]\n"
    msgb "选项:"
    echo "  install        完整安装（服务+Web显示）"
    echo "  install-web    仅Web注入（PVE升级后web被覆盖时使用）"
    echo "  uninstall      完整卸载"
    echo ""
    exit 1
}

##################### 主程序入口 #######################

# 处理命令行参数
if [[ $# -eq 0 ]]; then
    show_usage
fi

case "$1" in
    install)
        cmd_install
        ;;
    install-web)
        cmd_install_web
        ;;
    uninstall)
        cmd_uninstall
        ;;
    *)
        warn "未知选项: $1"
        show_usage
        ;;
esac
