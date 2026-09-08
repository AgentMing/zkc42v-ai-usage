import CoreBluetooth
import Foundation

// MARK: - Device record
struct DeviceRecord: Codable {
    var uuid: String
    var name: String
    var localName: String
    var rssi: Int
    var services: [String]
    var manufacturerData: String
    var rawAdvertisement: String
    var firstSeen: Date
}

var scanRecords: [String: DeviceRecord] = [:]

// MARK: - Scan mode
class Scanner: NSObject, CBCentralManagerDelegate {
    var manager: CBCentralManager!
    let duration: TimeInterval

    init(duration: TimeInterval) {
        self.duration = duration
        super.init()
    }

    func start() {
        manager = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let states = ["unknown", "resetting", "unsupported", "unauthorized", "poweredOff", "poweredOn"]
        let idx = Int(central.state.rawValue)
        print("BT state: \(idx < states.count ? states[idx] : "\(idx)")")
        if central.state == .poweredOn {
            central.scanForPeripherals(withServices: nil,
                options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
            print("scanning \(Int(duration))s, collecting all advertisement data ...")
            DispatchQueue.global().asyncAfter(deadline: .now() + duration) {
                self.finish()
            }
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        let key = peripheral.identifier.uuidString
        let name = peripheral.name ?? ""
        let localName = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? ""
        let services = (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID])?
            .map { $0.uuidString } ?? []
        let mfr = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data ?? Data()
        let mfrHex = mfr.map { String(format: "%02x", $0) }.joined()
        let raw = advertisementData.map { k, v in
            let val: String
            if let d = v as? Data { val = d.map { String(format: "%02x", $0) }.joined() }
            else if let a = v as? [CBUUID] { val = a.map { $0.uuidString }.joined(separator: ",") }
            else { val = "\(v)" }
            return "\(k)=\(val)"
        }.joined(separator: " | ")

        var rec = scanRecords[key] ?? DeviceRecord(
            uuid: key, name: "", localName: "", rssi: 0, services: [],
            manufacturerData: "", rawAdvertisement: "", firstSeen: Date())
        if !name.isEmpty { rec.name = name }
        if !localName.isEmpty { rec.localName = localName }
        rec.rssi = RSSI.intValue
        rec.manufacturerData = mfrHex
        if services.count > rec.services.count { rec.services = services }
        rec.rawAdvertisement = raw
        scanRecords[key] = rec
    }

    func finish() {
        manager.stopScan()
        let list = scanRecords.values.sorted { $0.rssi > $1.rssi }
        print("\n=== \(list.count) unique devices ===")
        for r in list {
            let svc = r.services.isEmpty ? "-" : r.services.joined(separator: ",")
            print(String(format: "%3d dBm | %-18@ | %-20@ | %@",
                         r.rssi, r.name as NSString, r.uuid as NSString, svc))
            if !r.manufacturerData.isEmpty {
                print("        mfr=\(r.manufacturerData)")
            }
        }
        // persist
        let dir = URL(fileURLWithPath: "data", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let ts = DateFormatter()
        ts.dateFormat = "yyyyMMdd-HHmmss"
        let file = dir.appendingPathComponent("scan-\(ts.string(from: Date())).json")
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        enc.dateEncodingStrategy = .iso8601
        if let data = try? enc.encode(list) {
            try? data.write(to: file)
            print("saved: \(file.path)")
        }
        exit(0)
    }
}

// MARK: - Inspect mode
class Inspector: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var manager: CBCentralManager!
    let target: String
    var peripheral: CBPeripheral?
    var output: [String: Any] = [:]
    var pendingServices = 0
    var discoveredServices = 0
    var notifyData: [String: [String]] = [:]

    init(target: String) {
        self.target = target
        super.init()
    }

    func start() {
        manager = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            central.scanForPeripherals(withServices: nil, options: nil)
            print("looking for \(target) ...")
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        guard peripheral.identifier.uuidString == target else { return }
        self.peripheral = peripheral
        central.stopScan()
        output["uuid"] = peripheral.identifier.uuidString
        output["name"] = peripheral.name ?? ""
        output["advertisement"] = advertisementData.map { k, v in
            let val: String
            if let d = v as? Data { val = d.map { String(format: "%02x", $0) }.joined() }
            else if let a = v as? [CBUUID] { val = a.map { $0.uuidString }.joined(separator: ",") }
            else { val = "\(v)" }
            return "\(k)=\(val)"
        }
        print("found \(target), connecting ...")
        peripheral.delegate = self
        central.connect(peripheral, options: nil)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("connected! discovering services ...")
        peripheral.discoverServices(nil)
    }

    func centralManager(_ central: CBCentralManager,
                        didFailToConnect peripheral: CBPeripheral, error: Error?) {
        print("connect failed: \(error?.localizedDescription ?? "unknown")")
        save()
        exit(1)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard error == nil else {
            print("service discovery error: \(error!.localizedDescription)")
            save(); exit(1)
        }
        var svcs: [[String: Any]] = []
        let svcList = peripheral.services ?? []
        pendingServices = svcList.count
        for s in svcList {
            svcs.append(["uuid": s.uuid.uuidString])
            peripheral.discoverCharacteristics(nil, for: s)
        }
        output["services"] = svcs
        if pendingServices == 0 {
            finishDump()
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        var svcs = (output["services"] as? [[String: Any]]) ?? []
        if let i = svcs.firstIndex(where: { ($0["uuid"] as? String) == service.uuid.uuidString }) {
            var chars: [[String: Any]] = []
            for c in service.characteristics ?? [] {
                var d: [String: Any] = [
                    "uuid": c.uuid.uuidString,
                    "properties": c.properties.rawValue,
                    "propNames": propNames(c.properties)
                ]
                if c.properties.contains(.read) {
                    peripheral.readValue(for: c)
                    d["reading"] = true
                }
                if c.properties.contains(.notify) || c.properties.contains(.indicate) {
                    peripheral.setNotifyValue(true, for: c)
                    d["subscribed"] = true
                }
                chars.append(d)
            }
            svcs[i]["characteristics"] = chars
            output["services"] = svcs
        }
        discoveredServices += 1
        if discoveredServices >= pendingServices {
            DispatchQueue.main.asyncAfter(deadline: .now() + 4.0) {
                self.finishDump()
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { return }
        let hex = (characteristic.value ?? Data()).map { String(format: "%02x", $0) }.joined()
        let asc = String(data: characteristic.value ?? Data(), encoding: .utf8) ?? ""
        var svcs = (output["services"] as? [[String: Any]]) ?? []
        outer: for i in svcs.indices {
            var chars = (svcs[i]["characteristics"] as? [[String: Any]]) ?? []
            for j in chars.indices where (chars[j]["uuid"] as? String) == characteristic.uuid.uuidString {
                chars[j]["value"] = hex
                chars[j]["ascii"] = asc
                chars[j].removeValue(forKey: "reading")
                svcs[i]["characteristics"] = chars
                break outer
            }
        }
        output["services"] = svcs
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { return }
        print("notify on: \(characteristic.uuid.uuidString)")
    }

    func finishDump() {
        output["notifyData"] = notifyData
        save()
        print("done")
        exit(0)
    }

    func propNames(_ p: CBCharacteristicProperties) -> String {
        var names: [String] = []
        if p.contains(.read) { names.append("read") }
        if p.contains(.write) { names.append("write") }
        if p.contains(.writeWithoutResponse) { names.append("writeNoResp") }
        if p.contains(.notify) { names.append("notify") }
        if p.contains(.indicate) { names.append("indicate") }
        if p.contains(.broadcast) { names.append("broadcast") }
        if p.contains(.extendedProperties) { names.append("extended") }
        return names.joined(separator: ",")
    }

    func save() {
        let dir = URL(fileURLWithPath: "data", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("profile-\(target).json")
        let data = try? JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
        try? data?.write(to: file)
        print("saved: \(file.path)")
        if let d = data, let s = String(data: d, encoding: .utf8) {
            print(s)
        }
    }
}

// MARK: - Cmd mode: write bytes to a characteristic and capture responses
class CmdProbe: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var manager: CBCentralManager!
    let target: String
    let writeCharUUID: String
    let payload: Data
    let duration: TimeInterval
    var peripheral: CBPeripheral?
    var pendingServices = 0
    var discoveredServices = 0
    var subscribed = 0
    var wrote = false
    var log: [String: Any] = [:]
    var events: [[String: Any]] = []

    init(target: String, writeCharUUID: String, payload: Data, duration: TimeInterval) {
        self.target = target
        self.writeCharUUID = writeCharUUID
        self.payload = payload
        self.duration = duration
        super.init()
    }

    func start() {
        manager = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            central.scanForPeripherals(withServices: nil, options: nil)
            print("looking for \(target) ...")
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        guard peripheral.identifier.uuidString == target else { return }
        self.peripheral = peripheral
        central.stopScan()
        print("found, connecting ...")
        peripheral.delegate = self
        central.connect(peripheral, options: nil)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("connected, discovering services ...")
        peripheral.discoverServices(nil)
    }

    func centralManager(_ central: CBCentralManager,
                        didFailToConnect peripheral: CBPeripheral, error: Error?) {
        print("connect failed: \(error?.localizedDescription ?? "unknown")")
        exit(1)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        let svcList = peripheral.services ?? []
        pendingServices = svcList.count
        for s in svcList {
            peripheral.discoverCharacteristics(nil, for: s)
        }
        if pendingServices == 0 { finish() }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for c in service.characteristics ?? [] {
            if c.properties.contains(.notify) || c.properties.contains(.indicate) {
                peripheral.setNotifyValue(true, for: c)
                subscribed += 1
            }
        }
        discoveredServices += 1
        if discoveredServices >= pendingServices {
            // write once everything is subscribed
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.doWrite(peripheral)
            }
        }
    }

    func doWrite(_ peripheral: CBPeripheral) {
        guard !wrote else { return }
        wrote = true
        // find target characteristic across all services
        for s in peripheral.services ?? [] {
            for c in s.characteristics ?? [] where c.uuid.uuidString == writeCharUUID {
                let hex = payload.map { String(format: "%02x", $0) }.joined()
                print("WRITE -> \(writeCharUUID): \(hex)")
                events.append(["dir": "write", "char": writeCharUUID, "data": hex, "t": Date()])
                if c.properties.contains(.write) {
                    peripheral.writeValue(payload, for: c, type: .withResponse)
                } else if c.properties.contains(.writeWithoutResponse) {
                    peripheral.writeValue(payload, for: c, type: .withoutResponse)
                } else {
                    print("target char has no write property")
                    finish()
                }
                return
            }
        }
        print("characteristic \(writeCharUUID) not found")
        finish()
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { return }
        let hex = (characteristic.value ?? Data()).map { String(format: "%02x", $0) }.joined()
        let asc = String(data: characteristic.value ?? Data(), encoding: .utf8) ?? ""
        print("[\(Date())] NTF <\(characteristic.uuid.uuidString): \(hex)\(asc.isEmpty ? "" : "  '\(asc)'")")
        events.append(["dir": "notify", "char": characteristic.uuid.uuidString, "data": hex, "t": Date()])
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let e = error {
            print("write error: \(e.localizedDescription)")
        } else {
            print("write OK (ack)")
        }
    }

    func finish() {
        log["target"] = target
        log["writeChar"] = writeCharUUID
        log["writeData"] = payload.map { String(format: "%02x", $0) }.joined()
        log["events"] = events
        let dir = URL(fileURLWithPath: "data", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("cmd-\(target)-\(writeCharUUID).json")
        if let d = try? JSONSerialization.data(withJSONObject: log, options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: file)
            print("saved: \(file.path)")
        }
        print("done")
        exit(0)
    }
}

// MARK: - Seq mode: one connection, sequential writes, full response capture
struct SeqWrite {
    let charUUID: String
    let data: Data
    let delayAfter: TimeInterval
}

class SeqProbe: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var manager: CBCentralManager!
    let target: String
    let writes: [SeqWrite]
    let listenAfter: TimeInterval
    let scanTimeout: TimeInterval
    var peripheral: CBPeripheral?
    var pendingServices = 0
    var discoveredServices = 0
    var events: [[String: Any]] = []
    var startTime = Date()

    init(target: String, writes: [SeqWrite], listenAfter: TimeInterval, scanTimeout: TimeInterval) {
        self.target = target
        self.writes = writes
        self.listenAfter = listenAfter
        self.scanTimeout = scanTimeout
        super.init()
    }

    func start() {
        startTime = Date()
        manager = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            central.scanForPeripherals(withServices: nil, options: nil)
            print("scanning for \(target) (timeout \(Int(scanTimeout))s) ...")
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        guard peripheral.identifier.uuidString == target else { return }
        self.peripheral = peripheral
        central.stopScan()
        print("FOUND \(peripheral.name ?? "") rssi=\(RSSI.intValue), connecting ...")
        peripheral.delegate = self
        central.connect(peripheral, options: nil)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("connected, discovering services ...")
        peripheral.discoverServices(nil)
    }

    func centralManager(_ central: CBCentralManager,
                        didFailToConnect peripheral: CBPeripheral, error: Error?) {
        print("connect failed: \(error?.localizedDescription ?? "unknown")")
        save()
        exit(1)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        let svcList = peripheral.services ?? []
        pendingServices = svcList.count
        for s in svcList {
            peripheral.discoverCharacteristics(nil, for: s)
        }
        if pendingServices == 0 { runWrites(peripheral) }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for c in service.characteristics ?? [] {
            if c.properties.contains(.notify) || c.properties.contains(.indicate) {
                peripheral.setNotifyValue(true, for: c)
            }
        }
        discoveredServices += 1
        if discoveredServices >= pendingServices {
            runWrites(peripheral)
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { return }
        let hex = (characteristic.value ?? Data()).map { String(format: "%02x", $0) }.joined()
        let asc = String(data: characteristic.value ?? Data(), encoding: .utf8) ?? ""
        print("[+\(Int(Date().timeIntervalSince(startTime)))s] NTF <\(characteristic.uuid.uuidString): \(hex)\(asc.isEmpty ? "" : "  '\(asc)'")")
        events.append(["dir": "notify", "char": characteristic.uuid.uuidString,
                       "data": hex, "t": Date().timeIntervalSince(startTime)])
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { return }
        print("notify on: \(characteristic.uuid.uuidString)")
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let e = error {
            print("write error: \(e.localizedDescription)")
        } else {
            print("write ack OK: \(characteristic.uuid.uuidString)")
        }
    }

    func runWrites(_ peripheral: CBPeripheral) {
        var idx = 0
        func next() {
            guard idx < writes.count else {
                print("all writes done, listening \(Int(listenAfter))s ...")
                DispatchQueue.global().asyncAfter(deadline: .now() + listenAfter) {
                    self.save()
                    exit(0)
                }
                return
            }
            let w = writes[idx]
            idx += 1
            if let c = findChar(peripheral, w.charUUID) {
                let hex = w.data.map { String(format: "%02x", $0) }.joined()
                print("[+\(Int(Date().timeIntervalSince(startTime)))s] WRITE -> \(w.charUUID): \(hex)")
                events.append(["dir": "write", "char": w.charUUID, "data": hex,
                               "t": Date().timeIntervalSince(startTime)])
                if c.properties.contains(.write) {
                    peripheral.writeValue(w.data, for: c, type: .withResponse)
                } else {
                    peripheral.writeValue(w.data, for: c, type: .withoutResponse)
                }
            } else {
                print("char \(w.charUUID) not found, skipping")
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + w.delayAfter) {
                next()
            }
        }
        next()
    }

    func findChar(_ peripheral: CBPeripheral, _ uuid: String) -> CBCharacteristic? {
        for s in peripheral.services ?? [] {
            for c in s.characteristics ?? [] where c.uuid.uuidString == uuid {
                return c
            }
        }
        return nil
    }

    func save() {
        let dir = URL(fileURLWithPath: "data", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("seq-\(target).json")
        let out: [String: Any] = ["target": target, "events": events]
        if let d = try? JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: file)
            print("saved: \(file.path)")
        }
    }
}

// MARK: - Send mode: flash an image frame to the tag (EPD-nRF5 0x30 framing)
class SendProbe: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var manager: CBCentralManager!
    let target: String
    let frameFile: String
    let oldFrameFile: String?
    var sendInit: Bool = false
    var initParam: Data? = nil
    var allowRedPartial: Bool = false
    var partialMaxArea: Double = 0.35
    var partialMaxRects: Int = 32
    var peripheral: CBPeripheral?
    var pendingServices = 0
    var discoveredServices = 0
    var events: [[String: Any]] = []
    var startTime = Date()
    var cmdChar: CBCharacteristic?

    struct Step {
        let data: Data
        let type: CBCharacteristicWriteType
        let delayAfter: TimeInterval
        let label: String?
    }

    struct Rect: Equatable {
        let x: Int
        let y: Int
        let w: Int
        let h: Int

        var xByte: Int { x / 8 }
        var byteWidth: Int { w / 8 }
        var area: Int { w * h }
    }

    init(target: String, frameFile: String, oldFrameFile: String? = nil) {
        self.target = target
        self.frameFile = frameFile
        self.oldFrameFile = oldFrameFile
        super.init()
    }

    func start() {
        startTime = Date()
        manager = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            central.scanForPeripherals(withServices: nil, options: nil)
            print("scanning for \(target) ...")
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        guard peripheral.identifier.uuidString == target else { return }
        self.peripheral = peripheral
        central.stopScan()
        print("FOUND, connecting ...")
        peripheral.delegate = self
        central.connect(peripheral, options: nil)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("connected, discovering services ...")
        peripheral.discoverServices(nil)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        let svcList = peripheral.services ?? []
        pendingServices = svcList.count
        for s in svcList {
            peripheral.discoverCharacteristics(nil, for: s)
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for c in service.characteristics ?? [] {
            if c.properties.contains(.notify) || c.properties.contains(.indicate) {
                peripheral.setNotifyValue(true, for: c)
            }
            if c.uuid.uuidString == "62750002-D828-918D-FB46-B6C11C675AEC" {
                cmdChar = c
            }
        }
        discoveredServices += 1
        if discoveredServices >= pendingServices {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.send(peripheral)
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { return }
        let hex = (characteristic.value ?? Data()).map { String(format: "%02x", $0) }.joined()
        let asc = String(data: characteristic.value ?? Data(), encoding: .utf8) ?? ""
        let shown = asc.isEmpty ? hex : "\(asc)  [\(hex)]"
        print("[+\(Int(Date().timeIntervalSince(startTime)))s] NTF <\(characteristic.uuid.uuidString): \(shown)")
        events.append(["dir": "notify", "char": characteristic.uuid.uuidString,
                       "data": hex, "t": Date().timeIntervalSince(startTime)])
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let e = error { print("write err: \(e.localizedDescription)") }
    }

    func send(_ peripheral: CBPeripheral) {
        guard let cmd = cmdChar else {
            print("command characteristic not found")
            exit(1)
        }
        guard let frame = readFrame() else {
            print("cannot read frame file")
            exit(1)
        }
        print("frame: \(frame.w)x\(frame.h), black \(frame.black.count)B red \(frame.red.count)B")

        var steps: [Step] = []

        let initModel: UInt8 = initParam?.first ?? 0x02
        var usedPartial = false
        if let oldPath = oldFrameFile {
            if let oldFrame = readFrame(at: oldPath) {
                if let plan = makePartialSteps(old: oldFrame, new: frame, initModel: initModel) {
                    if plan.rects.isEmpty {
                        print("frame unchanged; skipped BLE push")
                        save()
                        exit(0)
                    }
                    steps = plan.steps
                    usedPartial = true
                    let ratio = Double(plan.rects.reduce(0) { $0 + $1.area }) /
                        Double(frame.w * frame.h)
                    print("partial frame: rects=\(plan.rects.count), dirty area=\(String(format: "%.1f", ratio * 100))%")
                }
            } else {
                print("previous frame missing or invalid; falling back to full refresh")
            }
        }

        if !usedPartial {
            // Proven sequence for ZKC42V / GR5513 v1.10 (user-confirmed for clock/bigtest):
            //   INIT(0x01, model) → SET_SLOT(0x31,[0,slot]) → WRITE_IMG RLE → REFRESH(0x05)
            // Avoid extras that regress on this firmware:
            //   SLEEP(0x06)  → can leave full black after a good refresh
            //   SET_SLIDE    → unknown payload; empty slots are black RAM → flash then black
            //   SET_TIME     → force GUI; MODE_PICTURE = fill white
            steps.append(Step(data: Data([0x01, initModel]), type: .withResponse,
                              delayAfter: 0.20, label: "INIT model=0x\(String(format: "%02x", initModel))"))

            steps.append(Step(data: Data([0x31, 0x00, 0x00]), type: .withResponse,
                              delayAfter: 0.05, label: "SET_SLOT slot=0"))

            let blackRle = rleCompress(frame.black)
            let redRle = rleCompress(frame.red)
            print("+ black \(frame.black.count)B -> RLE \(blackRle.count)B, red \(frame.red.count)B -> RLE \(redRle.count)B")

            let chunk = 233
            var planeWrites: [(Data, CBCharacteristicWriteType)] = []
            appendRleChunks(&planeWrites, plane: blackRle, planeFlag: 0, chunk: chunk)
            appendRleChunks(&planeWrites, plane: redRle, planeFlag: 1, chunk: chunk)
            for (i, w) in planeWrites.enumerated() {
                let isLast = i == planeWrites.count - 1
                // Slightly longer pause after last plane so RAM is settled before REFRESH
                steps.append(Step(data: w.0, type: w.1,
                                  delayAfter: isLast ? 0.20 : 0.03, label: nil))
            }

            steps.append(Step(data: Data([0x05]), type: .withResponse,
                              delayAfter: 1.0, label: "REFRESH"))
        }

        print("total \(steps.count) steps, starting ...")
        for s in steps where s.label != nil {
            print("+ \(s.label!)")
        }

        var idx = 0
        func next() {
            guard idx < steps.count else {
                // Keep link up while the panel finishes the physical refresh (~1–3s)
                print("all writes done, listening 15s (hold BLE while EPD refreshes) ...")
                DispatchQueue.global().asyncAfter(deadline: .now() + 15) {
                    self.save()
                    exit(0)
                }
                return
            }
            let step = steps[idx]
            idx += 1
            if idx == 1 || idx == steps.count || idx % 50 == 0 {
                print("[+\(Int(Date().timeIntervalSince(startTime)))s] step \(idx)/\(steps.count)"
                      + (step.label.map { " (\($0))" } ?? ""))
            }
            peripheral.writeValue(step.data, for: cmd, type: step.type)
            DispatchQueue.global().asyncAfter(deadline: .now() + step.delayAfter) {
                next()
            }
        }
        next()
    }

    struct PartialPlan {
        let rects: [Rect]
        let steps: [Step]
    }

    struct Span {
        let start: Int
        let end: Int
        let y: Int
        let h: Int
    }

    func rawCommand(_ command: UInt8) -> Data {
        Data([0x03, command])
    }

    func rawData(_ data: Data) -> Data {
        var out = Data([0x04])
        out.append(data)
        return out
    }

    func u16LE(_ value: Int) -> Data {
        Data([UInt8(value & 0xff), UInt8((value >> 8) & 0x01)])
    }

    func appendWindowSteps(_ steps: inout [Step], rect: Rect, label: String) {
        let x0 = rect.xByte
        let x1 = x0 + rect.byteWidth - 1
        let y0 = rect.y
        let y1 = rect.y + rect.h - 1
        steps.append(Step(data: rawCommand(0x11), type: .withResponse,
                          delayAfter: 0.03, label: "\(label) DATA_ENTRY"))
        steps.append(Step(data: rawData(Data([0x03])), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
        steps.append(Step(data: rawCommand(0x44), type: .withResponse,
                          delayAfter: 0.03, label: "\(label) X_WINDOW"))
        steps.append(Step(data: rawData(Data([UInt8(x0), UInt8(x1)])), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
        steps.append(Step(data: rawCommand(0x45), type: .withResponse,
                          delayAfter: 0.03, label: "\(label) Y_WINDOW"))
        var yWindow = u16LE(y0)
        yWindow.append(u16LE(y1))
        steps.append(Step(data: rawData(yWindow), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
        steps.append(Step(data: rawCommand(0x4E), type: .withResponse,
                          delayAfter: 0.03, label: "\(label) X_COUNTER"))
        steps.append(Step(data: rawData(Data([UInt8(x0)])), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
        steps.append(Step(data: rawCommand(0x4F), type: .withResponse,
                          delayAfter: 0.03, label: "\(label) Y_COUNTER"))
        steps.append(Step(data: rawData(u16LE(y0)), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
    }

    func extractPlane(_ plane: Data, frameWidth: Int, frameHeight: Int, rect: Rect) -> Data {
        let rowBytes = frameWidth / 8
        var out = Data()
        for row in rect.y..<(rect.y + rect.h) {
            let start = row * rowBytes + rect.xByte
            out.append(plane.subdata(in: start..<(start + rect.byteWidth)))
        }
        return out
    }

    func appendPlaneDataSteps(_ steps: inout [Step], data: Data, label: String) {
        let chunk = 233
        var offset = 0
        while offset < data.count {
            let length = min(chunk, data.count - offset)
            let part = data.subdata(in: offset..<(offset + length))
            steps.append(Step(data: rawData(part), type: .withoutResponse,
                              delayAfter: 0.03, label: "\(label) DATA \(length)B"))
            offset += length
        }
    }

    func diffRects(old: Frame, new: Frame) -> [Rect] {
        guard old.w == new.w, old.h == new.h, old.w % 8 == 0 else { return [] }
        let rowBytes = old.w / 8
        var active: [Span] = []
        var finished: [Span] = []

        for y in 0..<old.h {
            var runs: [(Int, Int)] = []
            var x = 0
            let offset = y * rowBytes
            while x < rowBytes {
                let dirty = old.black[offset + x] != new.black[offset + x]
                    || old.red[offset + x] != new.red[offset + x]
                if !dirty {
                    x += 1
                    continue
                }
                let start = x
                x += 1
                while x < rowBytes {
                    if old.black[offset + x] == new.black[offset + x]
                        && old.red[offset + x] == new.red[offset + x] { break }
                    x += 1
                }
                runs.append((start, x))
            }

            var next: [Span] = []
            var used = Set<Int>()
            for run in runs {
                if let match = active.enumerated().first(where: { idx, previous in
                    !used.contains(idx) && run.0 < previous.end && previous.start < run.1
                }) {
                    let previous = match.element
                    used.insert(match.offset)
                    next.append(Span(start: min(run.0, previous.start),
                                     end: max(run.1, previous.end),
                                     y: previous.y,
                                     h: y - previous.y + 1))
                } else {
                    next.append(Span(start: run.0, end: run.1, y: y, h: 1))
                }
            }
            for (idx, previous) in active.enumerated() where !used.contains(idx) {
                finished.append(previous)
            }
            active = next
        }
        finished.append(contentsOf: active)
        return finished.sorted { $0.y == $1.y ? $0.start < $1.start : $0.y < $1.y }
            .map { Rect(x: $0.start * 8, y: $0.y, w: ($0.end - $0.start) * 8, h: $0.h) }
    }

    func makePartialSteps(old: Frame, new: Frame, initModel: UInt8) -> PartialPlan? {
        guard old.w == new.w, old.h == new.h, old.w % 8 == 0 else {
            print("partial frame dimensions are incompatible; falling back to full refresh")
            return nil
        }
        let rects = diffRects(old: old, new: new)
        if rects.isEmpty { return PartialPlan(rects: [], steps: []) }

        let redChanged = old.red != new.red
        if redChanged && !allowRedPartial {
            print("red plane changed; conservative full-refresh fallback (use --allow-red to test partial BWR)")
            return nil
        }
        let area = rects.reduce(0) { $0 + $1.area }
        let ratio = Double(area) / Double(new.w * new.h)
        if ratio > partialMaxArea || rects.count > partialMaxRects {
            print("partial dirty area/rectangle count exceeds limits; falling back to full refresh")
            return nil
        }

        var steps: [Step] = [
            Step(data: Data([0x01, initModel]), type: .withResponse,
                 delayAfter: 0.20, label: "INIT model=0x\(String(format: "%02x", initModel))"),
            Step(data: Data([0x31, 0x00, 0x00]), type: .withResponse,
                 delayAfter: 0.05, label: "SET_SLOT slot=0"),
        ]
        for (index, rect) in rects.enumerated() {
            let prefix = "RECT \(index + 1)/\(rects.count) \(rect.x),\(rect.y) \(rect.w)x\(rect.h)"
            for (name, command, plane) in [("BW", UInt8(0x24), new.black), ("RED", UInt8(0x26), new.red)] {
                let label = "\(prefix) \(name)"
                appendWindowSteps(&steps, rect: rect, label: label)
                steps.append(Step(data: rawCommand(command), type: .withResponse,
                                  delayAfter: 0.03, label: "\(label) RAM"))
                appendPlaneDataSteps(&steps,
                                     data: extractPlane(plane, frameWidth: new.w, frameHeight: new.h, rect: rect),
                                     label: label)
            }
        }
        steps.append(Step(data: rawCommand(0x21), type: .withResponse,
                          delayAfter: 0.03, label: "UPDATE_CTRL1"))
        steps.append(Step(data: rawData(Data([0x80, 0x00])), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
        steps.append(Step(data: rawCommand(0x22), type: .withResponse,
                          delayAfter: 0.03, label: "UPDATE_CTRL2 0xff"))
        steps.append(Step(data: rawData(Data([0xff])), type: .withoutResponse,
                          delayAfter: 0.03, label: nil))
        steps.append(Step(data: rawCommand(0x20), type: .withResponse,
                          delayAfter: 1.0, label: "MASTER_ACTIVATE"))
        return PartialPlan(rects: rects, steps: steps)
    }

    // RLE compression matching epdiy.cn rle.js:
    // repeat run: control = 0x80 | (len - 3); literal: control = (len - 1)
    func rleCompress(_ data: Data) -> Data {
        var out = Data()
        var i = 0
        let n = data.count
        while i < n {
            var runLen = 1
            while i + runLen < n && runLen < 130 && data[i + runLen] == data[i] {
                runLen += 1
            }
            if runLen >= 3 {
                out.append(UInt8(0x80 | (runLen - 3)))
                out.append(data[i])
                i += runLen
            } else {
                let start = i
                var len = 0
                while i < n && len < 127 {
                    if i + 2 < n && data[i] == data[i+1] && data[i] == data[i+2] {
                        break
                    }
                    len += 1
                    i += 1
                }
                if len == 0 {
                    out.append(UInt8(0x00))
                    out.append(data[i])
                    i += 1
                } else {
                    out.append(UInt8(len - 1))
                    for k in start..<(start + len) {
                        out.append(data[k])
                    }
                }
            }
        }
        return out
    }

    func appendRleChunks(_ writes: inout [(Data, CBCharacteristicWriteType)],
                         plane: Data, planeFlag: UInt8, chunk: Int) {
        // split RLE stream at code boundaries; each chunk is a valid RLE stream
        var chunks: [Data] = []
        var i = 0
        var start = 0
        while i < plane.count {
            let control = plane[i]
            let codeLen = (control & 0x80) != 0 ? 2 : (Int(control) + 2)
            if i - start + codeLen > chunk && i > start {
                chunks.append(plane.subdata(in: start..<i))
                start = i
            }
            i += codeLen
        }
        if i > start {
            chunks.append(plane.subdata(in: start..<i))
        }
        for (idx, c) in chunks.enumerated() {
            let flag: UInt8 = 0x04 | planeFlag | (idx == 0 ? 0x02 : 0x00)
            var f = Data([0x30, flag])
            f.append(c)
            writes.append((f, .withoutResponse))
        }
    }

    func appendChunks(_ writes: inout [(Data, CBCharacteristicWriteType)],
                      plane: Data, beginFlag: UInt8, contFlag: UInt8, chunk: Int) {
        var offset = 0
        var first = true
        while offset < plane.count {
            let len = min(chunk, plane.count - offset)
            var frame = Data([0x30, first ? beginFlag : contFlag])
            frame.append(plane.subdata(in: offset..<(offset + len)))
            writes.append((frame, first ? .withoutResponse : .withoutResponse))
            offset += len
            first = false
        }
    }

    struct Frame {
        var w: Int
        var h: Int
        var black: Data
        var red: Data
    }

    func readFrame(at path: String) -> Frame? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              data.count > 10,
              String(data: data[0..<7], encoding: .utf8) == "ZKEPD1\n" else {
            return nil
        }
        let w = Int(data[7]) | (Int(data[8]) << 8)
        let h = Int(data[9]) | (Int(data[10]) << 8)
        guard w > 0, h > 0, w % 8 == 0 else { return nil }
        let planeBytes = (w * h) / 8
        guard data.count == 11 + planeBytes * 2 else { return nil }
        return Frame(w: w, h: h,
                     black: data.subdata(in: 11..<(11 + planeBytes)),
                     red: data.subdata(in: (11 + planeBytes)..<(11 + planeBytes * 2)))
    }

    func readFrame() -> Frame? {
        readFrame(at: frameFile)
    }

    func save() {
        let dir = URL(fileURLWithPath: "data", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("send-\(target).json")
        let out: [String: Any] = ["target": target, "events": events]
        if let d = try? JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: file)
            print("saved: \(file.path)")
        }
    }
}

// MARK: - Server mode: emulate the official ZkongESL app (GATT server + advertise)
// The tag (as BLE central) scans for an advertiser carrying its MAC, connects,
// then receives image data as notifications on 0783b03e-...-5cb8.
class ServerProbe: NSObject, CBPeripheralManagerDelegate {
    var manager: CBPeripheralManager!
    let mac: String
    let frameFile: String?
    var dataQueue: [Data] = []
    var subscribed = false
    var notifyChar: CBMutableCharacteristic!
    var log: [String: Any] = [:]
    var events: [[String: Any]] = []

    init(mac: String, frameFile: String?) {
        self.mac = mac
        self.frameFile = frameFile
        super.init()
    }

    func start() {
        manager = CBPeripheralManager(delegate: self, queue: nil)
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        switch peripheral.state {
        case .poweredOn:
            log("BT poweredOn, adding service ...")
            addService()
        default:
            log("state: \(peripheral.state.rawValue)")
        }
    }

    func addService() {
        let svc = CBMutableService(type: CBUUID(string: "0783B03E-8535-B5A0-7140-A304D2495CB7"), primary: true)

        // 5cb8 notify (data downlink) — the one we notify on
        notifyChar = CBMutableCharacteristic(
            type: CBUUID(string: "0783B03E-8535-B5A0-7140-A304D2495CB8"),
            properties: [.notify], value: nil, permissions: [])
        // 5cb9 write/read/notify — tag ack path
        let wr = CBMutableCharacteristic(
            type: CBUUID(string: "0783B03E-8535-B5A0-7140-A304D2495CB9"),
            properties: [.write, .writeWithoutResponse, .read],
            value: nil, permissions: [.writeable, .readable])
        // 5cba write — secondary ack path
        let wr2 = CBMutableCharacteristic(
            type: CBUUID(string: "0783B03E-8535-B5A0-7140-A304D2495CBA"),
            properties: [.write, .writeWithoutResponse],
            value: nil, permissions: [.writeable])

        let cccd = CBUUID(string: "2902")
        notifyChar.descriptors = [CBMutableDescriptor(type: cccd, value: Data([0, 0]))]
        wr.descriptors = [CBMutableDescriptor(type: cccd, value: Data([0, 0]))]
        wr2.descriptors = [CBMutableDescriptor(type: cccd, value: Data([0, 0]))]

        svc.characteristics = [notifyChar, wr, wr2]
        manager.add(svc)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        if let e = error {
            log("add service error: \(e.localizedDescription)")
            return
        }
        log("service added, starting advertise ...")
        startAdvertising()
    }

    func startAdvertising() {
        let macBytes = mac.replacingOccurrences(of: ":", with: "")
        guard macBytes.count == 12 else {
            log("bad mac: \(mac)")
            return
        }
        var macArr: [UInt8] = []
        var i = macBytes.startIndex
        while i < macBytes.endIndex {
            let j = macBytes.index(i, offsetBy: 2)
            macArr.append(UInt8(macBytes[i..<j], radix: 16)!)
            i = j
        }
        // body = {0x06, 0x08, mac1..mac6}; crc = XOR(body)
        var body: [UInt8] = [0x06, 0x08]
        body.append(contentsOf: macArr)
        let crc = body.reduce(0) { $0 ^ $1 }
        // manufacturer data: company id (LE) = 0x06XX -> [crc, 0x06, 0x08, mac...]
        var mfr: [UInt8] = [crc, 0x06, 0x08]
        mfr.append(contentsOf: macArr)
        log("advertising mac=\(mac) mfr=\(mfr.map { String(format: "%02x", $0) }.joined())")

        manager.startAdvertising([
            CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: "0783B03E-8535-B5A0-7140-A304D2495CB7")],
            CBAdvertisementDataManufacturerDataKey: Data(mfr),
        ])
    }

    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        if let e = error {
            log("advertise error: \(e.localizedDescription)")
        } else {
            log("advertising ... waiting for tag to connect")
        }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveRead request: CBATTRequest) {
        log("READ request: char=\(request.characteristic.uuid.uuidString) off=\(request.offset)")
        request.value = Data([0x00])
        peripheral.respond(to: request, withResult: .success)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        for req in requests {
            let data = req.value ?? Data()
            let hex = data.map { String(format: "%02x", $0) }.joined()
            let asc = String(data: data, encoding: .utf8) ?? ""
            log("WRITE from tag: char=\(req.characteristic.uuid.uuidString) len=\(data.count) hex=\(hex)\(asc.isEmpty ? "" : " '\(asc)'")")
            events.append(["dir": "tagWrite", "char": req.characteristic.uuid.uuidString,
                           "len": data.count, "hex": hex, "t": Date()])
            // tag ack: if >= 35 it requests the next chunk
            if data.count >= 35 && !dataQueue.isEmpty {
                sendNext()
            }
        }
        peripheral.respond(to: requests.last!, withResult: .success)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager,
                           central: CBCentral,
                           didSubscribeTo characteristic: CBCharacteristic) {
        log("TAG SUBSCRIBED to \(characteristic.uuid.uuidString)")
        subscribed = true
        // tag connected + subscribed: begin pushing data if frame provided
        if let f = frameFile {
            prepareFrame(f)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                self.sendNext()
            }
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
                self.save(); exit(0)
            }
        }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager,
                           central: CBCentral,
                           didUnsubscribeFrom characteristic: CBCharacteristic) {
        log("TAG UNSUBSCRIBED")
        subscribed = false
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral, didConnect: Bool) {
        log("TAG \(didConnect ? "CONNECTED" : "DISCONNECTED")")
    }

    func prepareFrame(_ path: String) {
        guard let d = try? Data(contentsOf: URL(fileURLWithPath: path)),
              d.count > 10,
              String(data: d[0..<7], encoding: .utf8) == "ZKEPD1\n" else {
            log("bad frame file")
            return
        }
        let w = Int(d[7]) | (Int(d[8]) << 8)
        let h = Int(d[9]) | (Int(d[10]) << 8)
        let planeBytes = (w * h) / 8
        guard d.count == 11 + planeBytes * 2 else {
            log("frame size mismatch")
            return
        }
        let black = d.subdata(in: 11..<(11 + planeBytes))
        let red = d.subdata(in: (11 + planeBytes)..<(11 + planeBytes * 2))
        let chunk = 230
        // mimic app: sendDataList per plane, split into 230B chunks
        dataQueue = splitData(black, chunk)
        dataQueue.append(contentsOf: splitData(red, chunk))
        log("frame ready: \(w)x\(h), chunks=\(dataQueue.count)")
    }

    func splitData(_ data: Data, _ n: Int) -> [Data] {
        var out: [Data] = []
        var off = 0
        while off < data.count {
            let len = min(n, data.count - off)
            out.append(data.subdata(in: off..<(off + len)))
            off += len
        }
        return out
    }

    func sendNext() {
        guard !dataQueue.isEmpty else {
            log("all chunks sent")
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
                self.save(); exit(0)
            }
            return
        }
        let chunk = dataQueue.removeFirst()
        let ok = manager.updateValue(chunk, for: notifyChar, onSubscribedCentrals: nil)
        log("notify chunk (remaining \(dataQueue.count), delivered \(ok))")
        if !ok {
            // will retry on next write from tag or when readyToUpdate
            dataQueue.insert(chunk, at: 0)
        }
    }

    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) {
        log("ready to update, resuming ...")
        sendNext()
    }

    func log(_ s: String) {
        print("[server] \(s)")
    }

    func save() {
        log("saving ...")
        let dir = URL(fileURLWithPath: "data", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("server-\(mac).json")
        let out: [String: Any] = ["mac": mac, "events": events]
        if let d = try? JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: file)
            print("saved: \(file.path)")
        }
    }
}

// MARK: - main
let args = CommandLine.arguments
guard args.count >= 2 else {
    print("usage: \(args[0]) scan <seconds> | inspect <UUID>")
    exit(2)
}
switch args[1] {
case "scan":
    let dur = args.count >= 3 ? TimeInterval(args[2]) ?? 15 : 15
    let s = Scanner(duration: dur)
    s.start()
    RunLoop.current.run()
case "inspect":
    guard args.count >= 3 else { print("need UUID"); exit(2) }
    let i = Inspector(target: args[2])
    i.start()
    RunLoop.current.run()
case "cmd":
    // cmd <UUID> <writeCharHex> <hexdata> [duration]
    guard args.count >= 5 else {
        print("usage: \(args[0]) cmd <UUID> <writeCharHex> <hexdata> [duration]")
        exit(2)
    }
    let hexData = args[4].replacingOccurrences(of: " ", with: "")
    guard hexData.count % 2 == 0, !hexData.isEmpty else {
        print("bad hex data")
        exit(2)
    }
    var bytes: [UInt8] = []
    var i = hexData.startIndex
    while i < hexData.endIndex {
        let j = hexData.index(i, offsetBy: 2)
        bytes.append(UInt8(hexData[i..<j], radix: 16)!)
        i = j
    }
    let dur = args.count >= 6 ? TimeInterval(args[5]) ?? 6 : 6
    let c = CmdProbe(target: args[2], writeCharUUID: args[3],
                     payload: Data(bytes), duration: dur)
    c.start()
    DispatchQueue.global().asyncAfter(deadline: .now() + dur + 4) {
        c.finish()
    }
    RunLoop.current.run()
case "seq":
    // seq <UUID> <listenSeconds> <charUUID:hexdata> [<charUUID:hexdata> ...]
    guard args.count >= 5 else {
        print("usage: \(args[0]) seq <UUID> <listenSeconds> <char:hex> [<char:hex> ...]")
        exit(2)
    }
    let listen = TimeInterval(args[3]) ?? 5
    var writes: [SeqWrite] = []
    for pair in args.dropFirst(4) {
        let parts = pair.split(separator: ":", maxSplits: 1).map(String.init)
        guard parts.count == 2, !parts[1].isEmpty else {
            print("bad pair: \(pair)")
            exit(2)
        }
        let hexData = parts[1].replacingOccurrences(of: " ", with: "")
        guard hexData.count % 2 == 0 else { print("bad hex: \(parts[1])"); exit(2) }
        var bytes: [UInt8] = []
        var i = hexData.startIndex
        while i < hexData.endIndex {
            let j = hexData.index(i, offsetBy: 2)
            bytes.append(UInt8(hexData[i..<j], radix: 16)!)
            i = j
        }
        writes.append(SeqWrite(charUUID: parts[0], data: Data(bytes), delayAfter: 1.5))
    }
    let sp = SeqProbe(target: args[2], writes: writes, listenAfter: listen, scanTimeout: 50)
    sp.start()
    RunLoop.current.run()
case "send":
    // send <UUID> <framefile> [--init] [--initparam <hex>]
    guard args.count >= 4 else {
        print("usage: \(args[0]) send <UUID> <framefile> [--init] [--initparam <hex>]")
        exit(2)
    }
    var sp = SendProbe(target: args[2], frameFile: args[3])
    var i = 4
    while i < args.count {
        switch args[i] {
        case "--init":
            sp.sendInit = true
            i += 1
        case "--initparam":
            guard i + 1 < args.count else { print("--initparam needs hex"); exit(2) }
            let hex = args[i + 1].replacingOccurrences(of: " ", with: "")
            guard hex.count % 2 == 0, !hex.isEmpty else { print("bad hex"); exit(2) }
            var bytes: [UInt8] = []
            var j = hex.startIndex
            while j < hex.endIndex {
                let k = hex.index(j, offsetBy: 2)
                bytes.append(UInt8(hex[j..<k], radix: 16)!)
                j = k
            }
            sp.initParam = Data(bytes)
            i += 2
        default:
            print("unknown option \(args[i])")
            exit(2)
        }
    }
    sp.start()
    RunLoop.current.run()
case "partial":
    // partial <UUID> <old-frame> <new-frame> [--allow-red] [--initparam <hex>]
    guard args.count >= 5 else {
        print("usage: \(args[0]) partial <UUID> <old-frame> <new-frame> [--allow-red] [--initparam <hex>]")
        exit(2)
    }
    var sp = SendProbe(target: args[2], frameFile: args[4], oldFrameFile: args[3])
    var i = 5
    while i < args.count {
        switch args[i] {
        case "--allow-red":
            sp.allowRedPartial = true
            i += 1
        case "--initparam":
            guard i + 1 < args.count else { print("--initparam needs hex"); exit(2) }
            let hex = args[i + 1].replacingOccurrences(of: " ", with: "")
            guard hex.count % 2 == 0, !hex.isEmpty else { print("bad hex"); exit(2) }
            var bytes: [UInt8] = []
            var j = hex.startIndex
            while j < hex.endIndex {
                let k = hex.index(j, offsetBy: 2)
                bytes.append(UInt8(hex[j..<k], radix: 16)!)
                j = k
            }
            sp.initParam = Data(bytes)
            i += 2
        case "--max-area":
            guard i + 1 < args.count, let value = Double(args[i + 1]), value > 0, value <= 1 else {
                print("bad --max-area")
                exit(2)
            }
            sp.partialMaxArea = value
            i += 2
        case "--max-rects":
            guard i + 1 < args.count, let value = Int(args[i + 1]), value > 0 else {
                print("bad --max-rects")
                exit(2)
            }
            sp.partialMaxRects = value
            i += 2
        default:
            print("unknown option \(args[i])")
            exit(2)
        }
    }
    sp.start()
    RunLoop.current.run()
case "server":
    // server <MAC> [framefile]
    guard args.count >= 3 else {
        print("usage: \(args[0]) server <MAC> [framefile]")
        exit(2)
    }
    let mac = args[2]
    let frame = args.count >= 4 ? args[3] : nil
    let sp = ServerProbe(mac: mac, frameFile: frame)
    sp.start()
    RunLoop.current.run()
default:
    print("unknown command \(args[1])")
    exit(2)
}
