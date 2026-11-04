`timescale 1ns/1ps
`include "npu_types.sv"

module tb_pe_ws;
    localparam int CLK_HALF_NS = 5;
    localparam int WATCHDOG_NS  = 10_000_000;

    logic   clk;
    logic   rst;
    logic   pipe_en;
    logic   weight_we;
    int8_t  weight_in;
    int8_t  act_in;
    logic   act_valid_in;
    int32_t psum_in;
    logic   psum_valid_in;
    int8_t  act_out;
    logic   act_valid_out;
    int32_t psum_out;
    logic   psum_valid_out;

    int8_t  exp_weight;
    int8_t  exp_act_out;
    logic   exp_act_valid_out;
    int32_t exp_psum_out;
    logic   exp_psum_valid_out;

    int cycles;
    int checks;
    int protocol_checks;

    int8_t  seq_act    [0:5];
    int32_t seq_psum   [0:5];
    int8_t  bubble_act [0:2];
    int32_t bubble_psum[0:2];
    int8_t  resume_act [0:3];
    int32_t resume_psum[0:3];
    int8_t  pause_weight[0:3];
    int8_t  pause_act  [0:3];
    int32_t pause_psum [0:3];
    int8_t  post_act   [0:2];
    int32_t post_psum  [0:2];

    pe_ws dut (
        .clk            (clk),
        .rst            (rst),
        .pipe_en        (pipe_en),
        .weight_we      (weight_we),
        .weight_in      (weight_in),
        .act_in         (act_in),
        .act_valid_in   (act_valid_in),
        .psum_in        (psum_in),
        .psum_valid_in  (psum_valid_in),
        .act_out        (act_out),
        .act_valid_out  (act_valid_out),
        .psum_out       (psum_out),
        .psum_valid_out (psum_valid_out)
    );

    initial clk = 1'b0;
    always #(CLK_HALF_NS) clk <= ~clk;

    initial begin
        $dumpfile("pe.vcd");
        $dumpvars(0, tb_pe_ws);
    end

    initial begin : watchdog
        #(WATCHDOG_NS);
        $display("PE_FAIL reason=watchdog_timeout");
        $fatal(1, "watchdog timeout");
    end

    task automatic drive(
        input logic   rst_i,
        input logic   pipe_en_i,
        input logic   weight_we_i,
        input int8_t  weight_i,
        input int8_t  act_i,
        input logic   act_valid_i,
        input int32_t psum_i,
        input logic   psum_valid_i
    );
        @(negedge clk);
        rst           = rst_i;
        pipe_en       = pipe_en_i;
        weight_we     = weight_we_i;
        weight_in     = weight_i;
        act_in        = act_i;
        act_valid_in  = act_valid_i;
        psum_in       = psum_i;
        psum_valid_in = psum_valid_i;
    endtask

    task automatic check_protocol;
        if (!rst && weight_we && pipe_en) begin
            $display("PE_PROTOCOL_FAIL cycle=%0d reason=weight_we_and_pipe_en weight_we=%0b pipe_en=%0b",
                     cycles, weight_we, pipe_en);
            $fatal(1, "protocol violation: weight_we and pipe_en");
        end
        if (!rst && (act_valid_in !== psum_valid_in)) begin
            if (act_valid_in)
                $display("PE_PROTOCOL_FAIL cycle=%0d reason=act_valid_without_psum act_valid=%0b psum_valid=%0b",
                         cycles, act_valid_in, psum_valid_in);
            else
                $display("PE_PROTOCOL_FAIL cycle=%0d reason=psum_valid_without_act act_valid=%0b psum_valid=%0b",
                         cycles, act_valid_in, psum_valid_in);
            $fatal(1, "protocol violation: valid mismatch");
        end
        protocol_checks++;
    endtask

    task automatic update_expected(
        input logic   rst_i,
        input logic   pipe_en_i,
        input logic   weight_we_i,
        input int8_t  weight_i,
        input int8_t  act_i,
        input logic   act_valid_i,
        input int32_t psum_i,
        input logic   psum_valid_i
    );
        longint product_wide;
        longint psum_wide;

        if (rst_i) begin
            exp_weight        = 8'sh00;
            exp_act_out       = 8'sh00;
            exp_act_valid_out = 1'b0;
            exp_psum_out      = 32'sh0000_0000;
            exp_psum_valid_out = 1'b0;
        end else if (weight_we_i) begin
            exp_weight = weight_i;
        end else if (pipe_en_i) begin
            exp_act_out        = act_i;
            exp_act_valid_out  = act_valid_i;
            exp_psum_valid_out = psum_valid_i;
            if (act_valid_i && psum_valid_i) begin
                product_wide = $signed(act_i) * $signed(exp_weight);
                psum_wide    = {{32{psum_i[31]}}, psum_i} + product_wide;
                if (psum_wide > 64'sd2147483647 ||
                    psum_wide < -64'sd2147483648) begin
                    $display("PE_FAIL reason=expected_psum_overflow cycle=%0d psum_wide=%0d",
                             cycles, psum_wide);
                    $fatal(1, "test case leaves the non-overflowing INT32 range");
                end
                exp_psum_out = psum_wide[31:0];
            end else begin
                exp_psum_out = psum_i;
            end
        end
    endtask

    task automatic check_outputs;
        int errors;
        errors = 0;

        if (act_out !== exp_act_out) begin
            $display("PE_FAIL reason=output_mismatch cycle=%0d field=act_out got_act=%02h expected_act=%02h",
                     cycles, act_out, exp_act_out);
            errors++;
        end
        checks++;

        if (act_valid_out !== exp_act_valid_out) begin
            $display("PE_FAIL reason=output_mismatch cycle=%0d field=act_valid_out got_act_valid=%0b expected_act_valid=%0b",
                     cycles, act_valid_out, exp_act_valid_out);
            errors++;
        end
        checks++;

        if (psum_out !== exp_psum_out) begin
            $display("PE_FAIL reason=output_mismatch cycle=%0d field=psum_out got_psum=%08h expected_psum=%08h",
                     cycles, psum_out, exp_psum_out);
            errors++;
        end
        checks++;

        if (psum_valid_out !== exp_psum_valid_out) begin
            $display("PE_FAIL reason=output_mismatch cycle=%0d field=psum_valid_out got_psum_valid=%0b expected_psum_valid=%0b",
                     cycles, psum_valid_out, exp_psum_valid_out);
            errors++;
        end
        checks++;

        if (errors != 0)
            $fatal(1, "PE output mismatch");
    endtask

    task automatic run_cycle(
        input logic   rst_i,
        input logic   pipe_en_i,
        input logic   weight_we_i,
        input int8_t  weight_i,
        input int8_t  act_i,
        input logic   act_valid_i,
        input int32_t psum_i,
        input logic   psum_valid_i
    );
        drive(rst_i, pipe_en_i, weight_we_i, weight_i, act_i,
              act_valid_i, psum_i, psum_valid_i);
        check_protocol();
        update_expected(rst_i, pipe_en_i, weight_we_i, weight_i, act_i,
                        act_valid_i, psum_i, psum_valid_i);
        @(posedge clk);
        #1;
        check_outputs();
        cycles++;
    endtask

    initial begin : main
        rst           = 1'b1;
        pipe_en       = 1'b0;
        weight_we     = 1'b0;
        weight_in     = 8'sh00;
        act_in        = 8'sh00;
        act_valid_in  = 1'b0;
        psum_in       = 32'sh0000_0000;
        psum_valid_in = 1'b0;

        exp_weight         = 8'sh00;
        exp_act_out        = 8'sh00;
        exp_act_valid_out  = 1'b0;
        exp_psum_out       = 32'sh0000_0000;
        exp_psum_valid_out = 1'b0;

        cycles          = 0;
        checks          = 0;
        protocol_checks = 0;

        seq_act[0] = 8'sd1; seq_psum[0] = 32'sd0;
        seq_act[1] = 8'sd2; seq_psum[1] = 32'sd100;
        seq_act[2] = 8'sd3; seq_psum[2] = 32'sd200;
        seq_act[3] = 8'sd4; seq_psum[3] = 32'sd300;
        seq_act[4] = 8'sd5; seq_psum[4] = 32'sd400;
        seq_act[5] = 8'sd6; seq_psum[5] = 32'sd500;

        bubble_act[0] = 8'sd7; bubble_psum[0] = 32'sd600;
        bubble_act[1] = 8'sd8; bubble_psum[1] = -32'sd700;
        bubble_act[2] = 8'sd9; bubble_psum[2] = 32'sd800;

        resume_act[0] = 8'sd1; resume_psum[0] = 32'sd0;
        resume_act[1] = 8'sd2; resume_psum[1] = 32'sd100;
        resume_act[2] = 8'sd3; resume_psum[2] = 32'sd200;
        resume_act[3] = 8'sd4; resume_psum[3] = 32'sd300;

        pause_weight[0] = 8'sd11; pause_act[0] = 8'sd5; pause_psum[0] = 32'sd900;
        pause_weight[1] = 8'sd22; pause_act[1] = 8'sd6; pause_psum[1] = 32'sd1000;
        pause_weight[2] = 8'sd33; pause_act[2] = 8'sd7; pause_psum[2] = 32'sd1100;
        pause_weight[3] = 8'sd44; pause_act[3] = 8'sd8; pause_psum[3] = 32'sd1200;

        post_act[0] = 8'sd1; post_psum[0] = 32'sd0;
        post_act[1] = 8'sd2; post_psum[1] = 32'sd100;
        post_act[2] = 8'sd3; post_psum[2] = 32'sd200;

        // ---- 独立负路径：强制 X/Z，检查 scoreboard 真实判败 ----
        if ($test$plusargs("inject_x")) begin
            run_cycle(1'b1, 1'b0, 1'b0, 8'h00, 8'h00, 1'b0, 32'sd0, 1'b0);
`ifndef VERILATOR
            force dut.psum_out = 32'hxxxxxxxx;
            check_outputs();
`endif
            $display("PE_FAIL reason=inject_x_not_detected");
            $fatal(1, "X injection not detected");
        end
        if ($test$plusargs("inject_z")) begin
            run_cycle(1'b1, 1'b0, 1'b0, 8'h00, 8'h00, 1'b0, 32'sd0, 1'b0);
`ifndef VERILATOR
            force dut.psum_out = 32'hzzzzzzzz;
            check_outputs();
`endif
            $display("PE_FAIL reason=inject_z_not_detected");
            $fatal(1, "Z injection not detected");
        end

        // ---- 独立负路径：valid 不匹配与非法控制重叠 ----
        if ($test$plusargs("inject_act_only")) begin
            run_cycle(1'b1, 1'b0, 1'b0, 8'h00, 8'h00, 1'b0, 32'sd0, 1'b0);
            drive(1'b0, 1'b1, 1'b0, 8'h05, 8'h01, 1'b1, 32'sd0, 1'b0);
            check_protocol();
            $display("PE_FAIL reason=act_only_not_detected");
            $fatal(1, "act-only valid mismatch not detected");
        end
        if ($test$plusargs("inject_psum_only")) begin
            run_cycle(1'b1, 1'b0, 1'b0, 8'h00, 8'h00, 1'b0, 32'sd0, 1'b0);
            drive(1'b0, 1'b1, 1'b0, 8'h05, 8'h01, 1'b0, 32'sd0, 1'b1);
            check_protocol();
            $display("PE_FAIL reason=psum_only_not_detected");
            $fatal(1, "psum-only valid mismatch not detected");
        end
        if ($test$plusargs("inject_illegal_control")) begin
            run_cycle(1'b1, 1'b0, 1'b0, 8'h00, 8'h00, 1'b0, 32'sd0, 1'b0);
            drive(1'b0, 1'b1, 1'b1, 8'h05, 8'h01, 1'b0, 32'sd0, 1'b0);
            check_protocol();
            $display("PE_FAIL reason=illegal_control_not_detected");
            $fatal(1, "weight_we/pipe_en overlap not detected");
        end

        // ---- 复位与复位优先级：复位同时压过装载与推进 ----
        run_cycle(1'b1, 1'b0, 1'b0, 8'h11, 8'h22, 1'b1, 32'sd123, 1'b1);
        run_cycle(1'b1, 1'b0, 1'b0, 8'h33, 8'h44, 1'b1, 32'sd456, 1'b1);
        run_cycle(1'b1, 1'b1, 1'b0, 8'h55, 8'h66, 1'b1, 32'sd789, 1'b1);
        run_cycle(1'b1, 1'b0, 1'b1, 8'h5a, 8'h77, 1'b1, 32'sd321, 1'b1);

        // ---- 权重装载/保持/重载；计算时新的 weight_in 不得覆盖驻留值 ----
        run_cycle(1'b0, 1'b0, 1'b1, 8'sd5, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b0, 1'b0, 8'sd99, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'sd99, 8'sd3, 1'b1, 32'sd0, 1'b1);
        run_cycle(1'b0, 1'b1, 1'b0, 8'sd99, 8'sd2, 1'b1, 32'sd100, 1'b1);
        run_cycle(1'b0, 1'b0, 1'b0, 8'sd99, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b0, 1'b1, 8'shF9, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h00, 8'sd3, 1'b1, 32'sd10, 1'b1);

        // ---- 符号、极值与零权重/零激活 ----
        run_cycle(1'b0, 1'b0, 1'b1, 8'h80, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'h7f, 1'b1, 32'sd100000, 1'b1);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'h80, 1'b1, 32'sd123456789, 1'b1);
        run_cycle(1'b0, 1'b0, 1'b1, 8'h7f, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h80, 8'h7f, 1'b1, 32'shffff_ffff, 1'b1);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h80, 8'h80, 1'b1, -32'sd100000, 1'b1);
        run_cycle(1'b0, 1'b0, 1'b1, 8'h00, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'h80, 1'b1, -32'sd123456789, 1'b1);
        run_cycle(1'b0, 1'b0, 1'b1, 8'h80, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'h00, 1'b1, 32'sd987654321, 1'b1);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'h80, 1'b1, 32'sh7fff_bfff, 1'b1);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'h7f, 1'b1, 32'sh8000_3f80, 1'b1);

        // ---- 连续 valid、气泡、暂停与恢复 ----
        run_cycle(1'b0, 1'b0, 1'b1, 8'sd3, 8'h00, 1'b0, 32'sd0, 1'b0);
        for (int i = 0; i < 6; i++)
            run_cycle(1'b0, 1'b1, 1'b0, 8'h00, seq_act[i], 1'b1, seq_psum[i], 1'b1);
        for (int i = 0; i < 3; i++)
            run_cycle(1'b0, 1'b1, 1'b0, 8'h00, bubble_act[i], 1'b0, bubble_psum[i], 1'b0);
        for (int i = 0; i < 4; i++)
            run_cycle(1'b0, 1'b1, 1'b0, 8'h00, resume_act[i], 1'b1, resume_psum[i], 1'b1);
        for (int i = 0; i < 4; i++)
            run_cycle(1'b0, 1'b0, 1'b0, pause_weight[i], pause_act[i], 1'b0, pause_psum[i], 1'b0);
        for (int i = 0; i < 3; i++)
            run_cycle(1'b0, 1'b1, 1'b0, 8'h00, post_act[i], 1'b1, post_psum[i], 1'b1);

        // ---- 运行中复位：清权重、清流水、无残留 valid ----
        run_cycle(1'b1, 1'b1, 1'b0, 8'h00, 8'sd9, 1'b1, 32'sd777, 1'b1);
        run_cycle(1'b1, 1'b0, 1'b1, 8'sd42, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h00, 8'sd1, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, 8'sd7, 1'b1, 32'sd500, 1'b1);
        run_cycle(1'b0, 1'b0, 1'b1, 8'sd2, 8'h00, 1'b0, 32'sd0, 1'b0);
        run_cycle(1'b0, 1'b1, 1'b0, 8'h00, 8'sd9, 1'b1, 32'sd0, 1'b1);

        // ---- 256×256 全操作数：每个权重装载后穷举全部激活 ----
        for (int w = -128; w <= 127; w++) begin
            run_cycle(1'b0, 1'b0, 1'b1, int8_t'(w), 8'h00, 1'b0, 32'sd0, 1'b0);
            for (int a = -128; a <= 127; a++)
                run_cycle(1'b0, 1'b1, 1'b0, 8'h7f, int8_t'(a),
                          1'b1, 32'sd12345, 1'b1);
        end

        $display("PE_SUMMARY cycles=%0d output_checks=%0d protocol_checks=%0d",
                 cycles, checks, protocol_checks);
        $display("PE_PASS checks=%0d", checks);
        $finish;
    end
endmodule
