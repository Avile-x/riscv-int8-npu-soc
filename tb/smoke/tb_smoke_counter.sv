`timescale 1ns/1ps

module tb_smoke_counter;
    localparam int CLK_HALF_NS = 5;
    localparam int WATCHDOG_NS  = 100_000;

    typedef enum logic [2:0] {
        TEST_RESET, TEST_HOLD, TEST_INCREMENT,
        TEST_WRAP, TEST_RESET_PRIORITY, TEST_RESET_DURING_RUN, TEST_DONE
    } test_state_t;

    logic        clk;
    logic        rst;
    logic        en;
    logic [7:0]  count;
    logic [7:0]  expected;
    test_state_t state;
    int          checks;

    smoke_counter dut (
        .clk   (clk),
        .rst   (rst),
        .en    (en),
        .count (count)
    );

    initial clk = 1'b0;
    always #(CLK_HALF_NS) clk <= ~clk;

    initial begin
        $dumpfile("smoke.vcd");
        $dumpvars(0, tb_smoke_counter);
    end

    // 独立看门狗：仿真时间不推进或主流程卡住时失败。
    initial begin : watchdog
        #(WATCHDOG_NS);
        $display("SMOKE_FAIL watchdog_timeout");
        $fatal(1, "watchdog timeout");
    end

    task automatic drive(input logic rst_i, input logic en_i);
        @(negedge clk);
        rst = rst_i;
        en  = en_i;
    endtask

    task automatic check_count(input logic [7:0] expected_i);
        #1;
        if (count !== expected_i) begin
            $display("SMOKE_FAIL state=%0d got=%02h expected=%02h",
                     state, count, expected_i);
            $fatal(1, "count mismatch");
        end
        checks++;
    endtask

    initial begin
        clk      = 1'b0;
        rst      = 1'b1;
        en       = 1'b0;
        expected = 8'd0;
        checks   = 0;
        state    = TEST_RESET;

        // 四态检查器自检：故意把 DUT 输出强制为 X/Z，确认 !== 真的判败。
        // 二态工具不支持 force-Z，因此仅在 Icarus 四态路径编译。
`ifndef VERILATOR
        if ($test$plusargs("inject_x")) begin
            force dut.count = 8'hxx;
            check_count(8'h00);
            $display("CHECKER_BROKEN: X was not detected");
            $fatal(1, "X injection unexpectedly passed");
        end
        if ($test$plusargs("inject_z")) begin
            force dut.count = 8'hzz;
            check_count(8'h00);
            $display("CHECKER_BROKEN: Z was not detected");
            $fatal(1, "Z injection unexpectedly passed");
        end
`endif

        // 初始同步复位：连续两个上升沿都为 0。
        repeat (2) begin
            drive(1'b1, 1'b0);
            @(posedge clk);
            check_count(8'h00);
        end

        // 复位优先于使能：rst=1 且 en=1 仍清零。
        drive(1'b1, 1'b1);
        @(posedge clk);
        check_count(8'h00);

        // 释放复位后 en=0，保持 0。
        state = TEST_HOLD;
        repeat (3) begin
            drive(1'b0, 1'b0);
            @(posedge clk);
            check_count(8'h00);
        end

        // 连续递增 5 拍。
        state = TEST_INCREMENT;
        for (int i = 0; i < 5; i++) begin
            drive(1'b0, 1'b1);
            @(posedge clk);
            expected = expected + 8'd1;
            check_count(expected);
        end

        // 间歇递增：先保持 2 拍，再递增 3 拍，再保持，再递增 1 拍。
        repeat (2) begin
            drive(1'b0, 1'b0);
            @(posedge clk);
            check_count(expected);
        end
        for (int i = 0; i < 3; i++) begin
            drive(1'b0, 1'b1);
            @(posedge clk);
            expected = expected + 8'd1;
            check_count(expected);
        end
        drive(1'b0, 1'b0);
        @(posedge clk);
        check_count(expected);
        drive(1'b0, 1'b1);
        @(posedge clk);
        expected = expected + 8'd1;
        check_count(expected);

        // 从当前值递增 247 次，完整经过 255 并回绕到 0。
        state = TEST_WRAP;
        for (int i = 0; i < 247; i++) begin
            drive(1'b0, 1'b1);
            @(posedge clk);
            expected = expected + 8'd1;
            check_count(expected);
        end

        // en=1 时复位仍清零；释放后从 0 恢复计数。
        state = TEST_RESET_PRIORITY;
        drive(1'b1, 1'b1);
        @(posedge clk);
        expected = 8'd0;
        check_count(expected);
        drive(1'b1, 1'b1);
        @(posedge clk);
        check_count(expected);
        drive(1'b0, 1'b1);
        @(posedge clk);
        expected = expected + 8'd1;
        check_count(expected);
        drive(1'b0, 1'b1);
        @(posedge clk);
        expected = expected + 8'd1;
        check_count(expected);

        // 运行中再次复位，随后恢复计数。
        state = TEST_RESET_DURING_RUN;
        drive(1'b0, 1'b1);
        @(posedge clk);
        expected = expected + 8'd1;
        check_count(expected);
        drive(1'b1, 1'b1);
        @(posedge clk);
        expected = 8'd0;
        check_count(expected);
        drive(1'b1, 1'b1);
        @(posedge clk);
        check_count(expected);
        drive(1'b0, 1'b1);
        @(posedge clk);
        expected = expected + 8'd1;
        check_count(expected);
        drive(1'b0, 1'b0);
        @(posedge clk);
        check_count(expected);

        state = TEST_DONE;
        $display("SMOKE_PASS checks=%0d", checks);
        $finish;
    end
endmodule
