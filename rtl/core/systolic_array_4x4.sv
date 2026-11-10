`timescale 1ns/1ps
`include "npu_types.sv"

module systolic_array_4x4 (
    input  logic      clk,
    input  logic      rst,
    input  logic      req_valid,
    output logic      req_ready,
    input  matrix8_t  req_a,
    input  matrix8_t  req_b,
    output logic      rsp_valid,
    input  logic      rsp_ready,
    output matrix32_t rsp_c
);
    array_state_t state;
    logic [1:0]   load_row;
    logic [3:0]   issue_idx;
    logic [3:0]   result_idx;
    logic         rsp_valid_q;

    matrix8_t  a_buf;
    matrix8_t  b_buf;
    matrix32_t result_buf;

    logic       pipe_en;
    logic [3:0] weight_we;
    logic [1:0] issue_i;
    logic [1:0] issue_k;

    int8_t  act_data  [0:3][0:4];
    logic   act_valid [0:3][0:4];
    int32_t psum_data  [0:4][0:3];
    logic   psum_valid [0:4][0:3];

    int8_t  inject_act_data  [0:3];
    logic   inject_act_valid [0:3];
    int32_t inject_psum_data  [0:3];
    logic   inject_psum_valid [0:3];

    logic capture_hit;
    logic capture_last;

    assign req_ready = (state == IDLE) && !rst;
    assign rsp_valid = rsp_valid_q && !rst;
    assign pipe_en = (state == COMPUTE) || (state == DRAIN);
    assign issue_i = issue_idx[3:2];
    assign issue_k = issue_idx[1:0];

    always_comb begin
        weight_we = 4'b0000;
        if (state == LOAD_WEIGHTS)
            weight_we[load_row] = 1'b1;
    end

    always_comb begin
        for (int r = 0; r < 4; r++) begin
            inject_act_valid[r] = 1'b0;
            inject_act_data[r]  = 8'sd0;
            inject_psum_valid[r] = 1'b0;
            inject_psum_data[r]  = 32'sd0;
        end
        if (state == COMPUTE) begin
            inject_act_valid[issue_k] = 1'b1;
            inject_act_data[issue_k]  = a_buf[issue_i][issue_k];
            inject_psum_valid[issue_k] = 1'b1;
            inject_psum_data[issue_k]  = 32'sd0;
        end
    end

    assign capture_hit =
        ((state == COMPUTE) || (state == DRAIN)) &&
        psum_valid[4][result_idx[1:0]];
    assign capture_last = capture_hit && (result_idx == 4'd15);

    genvar r;
    genvar c;
    generate
        for (r = 0; r < 4; r++) begin : g_inject
            assign act_valid[r][0] = inject_act_valid[r];
            assign act_data[r][0]  = inject_act_data[r];
            assign psum_valid[0][r] = inject_psum_valid[r];
            assign psum_data[0][r]  = inject_psum_data[r];
        end
        for (r = 0; r < 4; r++) begin : g_row
            for (c = 0; c < 4; c++) begin : g_col
                pe_ws u_pe (
                    .clk           (clk),
                    .rst           (rst),
                    .pipe_en       (pipe_en),
                    .weight_we     (weight_we[r]),
                    .weight_in     (b_buf[r][c]),
                    .act_in        (act_data[r][c]),
                    .act_valid_in  (act_valid[r][c]),
                    .psum_in       (psum_data[r][c]),
                    .psum_valid_in (psum_valid[r][c]),
                    .act_out       (act_data[r][c+1]),
                    .act_valid_out (act_valid[r][c+1]),
                    .psum_out      (psum_data[r+1][c]),
                    .psum_valid_out(psum_valid[r+1][c])
                );
            end
        end
        for (r = 0; r < 4; r++) begin : g_rsp_row
            for (c = 0; c < 4; c++) begin : g_rsp_col
                assign rsp_c[r][c] = result_buf[r][c];
            end
        end
    endgenerate

    always_ff @(posedge clk) begin
        if (rst) begin
            state       <= IDLE;
            load_row    <= 2'd0;
            issue_idx   <= 4'd0;
            result_idx  <= 4'd0;
            rsp_valid_q <= 1'b0;
            for (int i = 0; i < 4; i++) begin
                for (int j = 0; j < 4; j++) begin
                    a_buf[i][j]      <= 8'sd0;
                    b_buf[i][j]      <= 8'sd0;
                    result_buf[i][j] <= 32'sd0;
                end
            end
        end else begin
            case (state)
                IDLE: begin
                    if (req_valid) begin
                        for (int i = 0; i < 4; i++) begin
                            for (int j = 0; j < 4; j++) begin
                                a_buf[i][j] <= req_a[i][j];
                                b_buf[i][j] <= req_b[i][j];
                            end
                        end
                        load_row   <= 2'd0;
                        issue_idx  <= 4'd0;
                        result_idx <= 4'd0;
                        state      <= LOAD_WEIGHTS;
                    end
                end
                LOAD_WEIGHTS: begin
                    if (load_row == 2'd3)
                        state <= COMPUTE;
                    else
                        load_row <= load_row + 2'd1;
                end
                COMPUTE: begin
                    if (capture_hit) begin
                        result_buf[result_idx[3:2]][result_idx[1:0]]
                            <= psum_data[4][result_idx[1:0]];
                        if (!capture_last)
                            result_idx <= result_idx + 4'd1;
                    end
                    if (issue_idx == 4'd15) begin
                        if (capture_last) begin
                            rsp_valid_q <= 1'b1;
                            state <= HOLD_RESULT;
                        end else begin
                            state <= DRAIN;
                        end
                    end else if (capture_last) begin
                        rsp_valid_q <= 1'b1;
                        state <= HOLD_RESULT;
                    end else begin
                        issue_idx <= issue_idx + 4'd1;
                    end
                end
                DRAIN: begin
                    if (capture_hit) begin
                        result_buf[result_idx[3:2]][result_idx[1:0]]
                            <= psum_data[4][result_idx[1:0]];
                        if (capture_last) begin
                            rsp_valid_q <= 1'b1;
                            state <= HOLD_RESULT;
                        end else begin
                            result_idx <= result_idx + 4'd1;
                        end
                    end
                end
                HOLD_RESULT: begin
                    if (rsp_ready) begin
                        rsp_valid_q <= 1'b0;
                        state <= IDLE;
                    end
                end
                default: begin
                    state <= IDLE;
                end
            endcase
        end
    end
endmodule
