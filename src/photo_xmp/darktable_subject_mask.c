/*
 * Native bridge for darktable's prompt-driven object segmentation.
 *
 * This program links to the user's installed libdarktable and calls the same
 * dt_seg_* and ras2forms APIs used by darktable 5.6's object-mask GUI. Its
 * prompt/refinement flow is derived in part from darktable's GPLv3+
 * src/develop/masks/object.c. It does not bundle a model or inference runtime.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int gboolean;
typedef struct dt_ai_environment_t dt_ai_environment_t;
typedef struct dt_seg_context_t dt_seg_context_t;
typedef struct GList
{
  void *data;
  struct GList *next;
  struct GList *prev;
} GList;

typedef struct dt_seg_point_t
{
  float x, y;
  int label;
} dt_seg_point_t;

typedef struct dt_masks_point_path_t
{
  float corner[2];
  float ctrl1[2];
  float ctrl2[2];
  float border[2];
  int state;
} dt_masks_point_path_t;

typedef struct dt_masks_form_t
{
  GList *points;
  int type;
  const void *functions;
  float source[2];
  char name[128];
  int formid;
  int version;
} dt_masks_form_t;

extern int dt_init(int argc, char *argv[], gboolean init_gui,
                   gboolean load_data, void *lua_state);
extern void dt_cleanup(void);
extern dt_ai_environment_t *dt_ai_env_init(const char *search_paths);
extern void dt_ai_env_destroy(dt_ai_environment_t *env);
extern dt_seg_context_t *dt_seg_load(dt_ai_environment_t *env,
                                     const char *model_id);
extern gboolean dt_seg_encode_image(dt_seg_context_t *ctx,
                                    const uint8_t *rgb_data, int width, int height);
extern float *dt_seg_compute_mask(dt_seg_context_t *ctx,
                                  const dt_seg_point_t *points, int n_points,
                                  int *out_width, int *out_height);
extern gboolean dt_seg_supports_box(dt_seg_context_t *ctx);
extern void dt_seg_reset_prev_mask(dt_seg_context_t *ctx);
extern void dt_seg_free(dt_seg_context_t *ctx);
extern GList *ras2forms(const float *mask, int width, int height,
                        const void *image, float threshold, int turdsize,
                        double alphamax, GList **out_signs);
extern void dt_masks_free_form(dt_masks_form_t *form);
extern void g_list_free(GList *list);
extern void g_free(void *memory);

typedef struct arguments_t
{
  const char *configdir, *cachedir, *datadir, *moduledir, *localedir;
  const char *model, *rgb_path, *mask_path, *paths_path;
  int width, height, passes, cleanup;
  float threshold, smoothing, feather;
  dt_seg_point_t *prompts;
  int n_prompts, prompt_capacity;
  float box[4];
  int has_box;
} arguments_t;

static void fail(const char *message)
{
  fprintf(stderr, "%s\n", message);
  exit(2);
}

static const char *need_value(int argc, char **argv, int *index)
{
  if(*index + 1 >= argc) fail("missing option value");
  return argv[++(*index)];
}

static void add_prompt(arguments_t *args, const float x, const float y, const int label)
{
  if(x < 0.0f || x > 1.0f || y < 0.0f || y > 1.0f)
    fail("prompt coordinates must be normalized to [0,1]");
  if(args->n_prompts == args->prompt_capacity)
  {
    args->prompt_capacity = args->prompt_capacity ? args->prompt_capacity * 2 : 8;
    args->prompts = realloc(args->prompts,
                            (size_t)args->prompt_capacity * sizeof(dt_seg_point_t));
    if(!args->prompts) fail("out of memory");
  }
  args->prompts[args->n_prompts++] = (dt_seg_point_t){ x, y, label };
}

static arguments_t parse_args(int argc, char **argv)
{
  if(argc == 2 && (!strcmp(argv[1], "--help") || !strcmp(argv[1], "-h")))
  {
    puts("darktable-subject-mask native helper");
    puts("loader smoke: ok");
    exit(0);
  }
  arguments_t args = { 0 };
  args.passes = 3;
  args.threshold = 0.5f;
  args.cleanup = 50;
  args.smoothing = 1.0f;
  args.feather = 0.01f;
  for(int i = 1; i < argc; i++)
  {
    const char *option = argv[i];
    if(!strcmp(option, "--configdir")) args.configdir = need_value(argc, argv, &i);
    else if(!strcmp(option, "--cachedir")) args.cachedir = need_value(argc, argv, &i);
    else if(!strcmp(option, "--datadir")) args.datadir = need_value(argc, argv, &i);
    else if(!strcmp(option, "--moduledir")) args.moduledir = need_value(argc, argv, &i);
    else if(!strcmp(option, "--localedir")) args.localedir = need_value(argc, argv, &i);
    else if(!strcmp(option, "--model")) args.model = need_value(argc, argv, &i);
    else if(!strcmp(option, "--rgb")) args.rgb_path = need_value(argc, argv, &i);
    else if(!strcmp(option, "--mask")) args.mask_path = need_value(argc, argv, &i);
    else if(!strcmp(option, "--paths")) args.paths_path = need_value(argc, argv, &i);
    else if(!strcmp(option, "--width")) args.width = atoi(need_value(argc, argv, &i));
    else if(!strcmp(option, "--height")) args.height = atoi(need_value(argc, argv, &i));
    else if(!strcmp(option, "--passes")) args.passes = atoi(need_value(argc, argv, &i));
    else if(!strcmp(option, "--threshold")) args.threshold = strtof(need_value(argc, argv, &i), NULL);
    else if(!strcmp(option, "--cleanup")) args.cleanup = atoi(need_value(argc, argv, &i));
    else if(!strcmp(option, "--smoothing")) args.smoothing = strtof(need_value(argc, argv, &i), NULL);
    else if(!strcmp(option, "--feather")) args.feather = strtof(need_value(argc, argv, &i), NULL);
    else if(!strcmp(option, "--foreground") || !strcmp(option, "--background"))
    {
      const int label = !strcmp(option, "--foreground") ? 1 : 0;
      const float x = strtof(need_value(argc, argv, &i), NULL);
      const float y = strtof(need_value(argc, argv, &i), NULL);
      add_prompt(&args, x, y, label);
    }
    else if(!strcmp(option, "--box"))
    {
      for(int j = 0; j < 4; j++)
        args.box[j] = strtof(need_value(argc, argv, &i), NULL);
      args.has_box = 1;
    }
    else fail("unknown native-helper option");
  }
  if(!args.configdir || !args.cachedir || !args.datadir || !args.moduledir
     || !args.localedir || !args.model || !args.rgb_path || !args.mask_path
     || !args.paths_path || args.width <= 0 || args.height <= 0)
    fail("missing required native-helper arguments");
  int foregrounds = 0;
  for(int i = 0; i < args.n_prompts; i++) foregrounds += args.prompts[i].label == 1;
  if(!foregrounds) fail("at least one foreground prompt is required");
  if(args.passes < 1 || args.passes > 3) fail("passes must be between 1 and 3");
  if(args.threshold < 0.3f || args.threshold > 0.9f) fail("threshold must be in [0.3,0.9]");
  if(args.cleanup < 0 || args.cleanup > 100) fail("cleanup must be in [0,100]");
  if(args.smoothing < 0.0f || args.smoothing > 1.3f) fail("smoothing must be in [0,1.3]");
  return args;
}

static uint8_t *read_rgb(const arguments_t *args)
{
  const size_t size = (size_t)args->width * args->height * 3;
  FILE *file = fopen(args->rgb_path, "rb");
  if(!file) fail("cannot open temporary RGB input");
  uint8_t *data = malloc(size);
  if(!data || fread(data, 1, size, file) != size) fail("cannot read temporary RGB input");
  if(fgetc(file) != EOF) fail("temporary RGB input has an unexpected size");
  fclose(file);
  return data;
}

static void add_pixel_prompt(dt_seg_point_t *points, int *count,
                             float x, float y, int label, int width, int height)
{
  points[*count].x = x * (float)(width - 1);
  points[*count].y = y * (float)(height - 1);
  points[*count].label = label;
  (*count)++;
}

static int bbox_from_mask(const float *mask, int width, int height, float threshold,
                          dt_seg_point_t *top_left, dt_seg_point_t *bottom_right)
{
  int min_x = width, min_y = height, max_x = -1, max_y = -1;
  for(int y = 0; y < height; y++)
    for(int x = 0; x < width; x++)
      if(mask[(size_t)y * width + x] > threshold)
      {
        if(x < min_x) min_x = x; if(x > max_x) max_x = x;
        if(y < min_y) min_y = y; if(y > max_y) max_y = y;
      }
  if(max_x < 0) return 0;
  const int pad_x = (max_x - min_x) / 20 + 1;
  const int pad_y = (max_y - min_y) / 20 + 1;
  *top_left = (dt_seg_point_t){ fmaxf(0, min_x - pad_x),
                                fmaxf(0, min_y - pad_y), 2 };
  *bottom_right = (dt_seg_point_t){ fminf(width - 1, max_x + pad_x),
                                    fminf(height - 1, max_y + pad_y), 3 };
  return 1;
}

static int peak_from_mask(const float *mask, int width, int height, float threshold,
                          const dt_seg_point_t *points, int count, dt_seg_point_t *peak)
{
  float best = threshold;
  int best_x = -1, best_y = -1;
  for(int y = 0; y < height; y += 2)
    for(int x = 0; x < width; x += 2)
    {
      const float value = mask[(size_t)y * width + x];
      if(value <= best) continue;
      int separated = 1;
      for(int i = 0; i < count; i++)
      {
        const float dx = points[i].x - x, dy = points[i].y - y;
        if(points[i].label == 1 && dx * dx + dy * dy < 256.0f)
        { separated = 0; break; }
      }
      if(separated) { best = value; best_x = x; best_y = y; }
    }
  if(best_x < 0) return 0;
  *peak = (dt_seg_point_t){ (float)best_x, (float)best_y, 1 };
  return 1;
}

/* Keep the threshold-connected component containing the most recent foreground seed. */
static void keep_seed_component(float *mask, int width, int height, float threshold,
                                int seed_x, int seed_y)
{
  const size_t count = (size_t)width * height;
  uint8_t *seen = calloc(count, 1);
  int *queue = malloc(count * sizeof(int));
  if(!seen || !queue) fail("out of memory while filtering mask");
  seed_x = seed_x < 0 ? 0 : seed_x >= width ? width - 1 : seed_x;
  seed_y = seed_y < 0 ? 0 : seed_y >= height ? height - 1 : seed_y;
  int seed = seed_y * width + seed_x;
  if(mask[seed] <= threshold)
  {
    float best = threshold;
    for(size_t i = 0; i < count; i++)
      if(mask[i] > best) { best = mask[i]; seed = (int)i; }
  }
  int head = 0, tail = 0;
  if(mask[seed] > threshold) { seen[seed] = 1; queue[tail++] = seed; }
  while(head < tail)
  {
    const int at = queue[head++], x = at % width, y = at / width;
    for(int dy = -1; dy <= 1; dy++)
      for(int dx = -1; dx <= 1; dx++)
      {
        const int nx = x + dx, ny = y + dy;
        if(nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
        const int next = ny * width + nx;
        if(!seen[next] && mask[next] > threshold)
        { seen[next] = 1; queue[tail++] = next; }
      }
  }
  for(size_t i = 0; i < count; i++) if(!seen[i]) mask[i] = 0.0f;
  free(queue); free(seen);
}

static void write_mask(const char *path, const float *mask, int width, int height)
{
  FILE *file = fopen(path, "wb");
  if(!file) fail("cannot create native mask output");
  const size_t count = (size_t)width * height;
  if(fwrite(mask, sizeof(float), count, file) != count) fail("cannot write native mask output");
  fclose(file);
}

static void write_paths(const arguments_t *args, const float *mask, int width, int height)
{
  const size_t count = (size_t)width * height;
  float *inverted = malloc(count * sizeof(float));
  if(!inverted) fail("out of memory while vectorizing mask");
  for(size_t i = 0; i < count; i++) inverted[i] = 1.0f - mask[i];
  GList *signs = NULL;
  GList *forms = ras2forms(inverted, width, height, NULL,
                           1.0f - args->threshold, args->cleanup,
                           args->smoothing, &signs);
  free(inverted);
  FILE *file = fopen(args->paths_path, "w");
  if(!file) fail("cannot create vector path output");
  fprintf(file, "{\"paths\":[");
  int path_index = 0;
  GList *sign = signs;
  for(GList *form_node = forms; form_node; form_node = form_node->next)
  {
    dt_masks_form_t *form = form_node->data;
    if(!form || !form->points) continue;
    const int marker = sign ? (int)(intptr_t)sign->data : '+';
    if(path_index++) fputc(',', file);
    fprintf(file, "{\"sign\":\"%c\",\"points\":[", marker == '-' ? '-' : '+');
    int point_index = 0;
    for(GList *point_node = form->points; point_node; point_node = point_node->next)
    {
      const dt_masks_point_path_t *point = point_node->data;
      if(point_index++) fputc(',', file);
      fprintf(file,
              "{\"corner\":[%.8g,%.8g],\"ctrl1\":[%.8g,%.8g],"
              "\"ctrl2\":[%.8g,%.8g],\"border\":[%.8g,%.8g],\"state\":2}",
              point->corner[0] / width, point->corner[1] / height,
              point->ctrl1[0] / width, point->ctrl1[1] / height,
              point->ctrl2[0] / width, point->ctrl2[1] / height,
              args->feather / width, args->feather / height);
    }
    fputs("]}", file);
    if(sign) sign = sign->next;
  }
  fputs("]}\n", file);
  fclose(file);
  for(GList *node = forms; node; node = node->next)
    dt_masks_free_form((dt_masks_form_t *)node->data);
  g_list_free(forms);
  g_list_free(signs);
  if(path_index == 0) fail("darktable vectorization produced no usable paths");
}

int main(int argc, char **argv)
{
  arguments_t args = parse_args(argc, argv);
  char *dt_argv[] = {
    argv[0], "--library", ":memory:",
    "--configdir", (char *)args.configdir,
    "--cachedir", (char *)args.cachedir,
    "--datadir", (char *)args.datadir,
    "--moduledir", (char *)args.moduledir,
    "--localedir", (char *)args.localedir, NULL
  };
  if(dt_init(13, dt_argv, 0, 0, NULL)) fail("darktable runtime initialization failed");
  dt_ai_environment_t *environment = dt_ai_env_init(NULL);
  if(!environment) fail("darktable AI is disabled or unavailable");
  dt_seg_context_t *segmentation = dt_seg_load(environment, args.model);
  if(!segmentation) fail("darktable could not load the active mask model");

  uint8_t *rgb = read_rgb(&args);
  if(!dt_seg_encode_image(segmentation, rgb, args.width, args.height))
    fail("darktable failed to encode the source image");
  free(rgb);

  const int capacity = args.n_prompts + 5;
  dt_seg_point_t *points = calloc((size_t)capacity, sizeof(dt_seg_point_t));
  if(!points) fail("out of memory");
  int n_points = 0, last_foreground_x = 0, last_foreground_y = 0;
  for(int i = 0; i < args.n_prompts; i++)
  {
    add_pixel_prompt(points, &n_points, args.prompts[i].x, args.prompts[i].y,
                     args.prompts[i].label, args.width, args.height);
    if(args.prompts[i].label == 1)
    {
      last_foreground_x = (int)points[n_points - 1].x;
      last_foreground_y = (int)points[n_points - 1].y;
    }
  }
  free(args.prompts);
  if(args.has_box)
  {
    if(!dt_seg_supports_box(segmentation)) fail("the active darktable model does not support box prompts");
    add_pixel_prompt(points, &n_points, args.box[0], args.box[1], 2, args.width, args.height);
    add_pixel_prompt(points, &n_points, args.box[2], args.box[3], 3, args.width, args.height);
  }

  dt_seg_reset_prev_mask(segmentation);
  float *mask = NULL;
  int mask_width = 0, mask_height = 0, refinement_box_added = args.has_box;
  for(int pass = 0; pass < args.passes; pass++)
  {
    float *next = dt_seg_compute_mask(segmentation, points, n_points,
                                      &mask_width, &mask_height);
    if(!next) break;
    g_free(mask); mask = next;
    if(pass + 1 == args.passes) break;
    dt_seg_point_t peak;
    if(peak_from_mask(mask, mask_width, mask_height, args.threshold,
                      points, n_points, &peak)) points[n_points++] = peak;
    if(dt_seg_supports_box(segmentation) && !refinement_box_added)
    {
      dt_seg_point_t top_left, bottom_right;
      if(bbox_from_mask(mask, mask_width, mask_height, args.threshold,
                        &top_left, &bottom_right))
      { points[n_points++] = top_left; points[n_points++] = bottom_right; refinement_box_added = 1; }
    }
  }
  free(points);
  if(!mask || mask_width != args.width || mask_height != args.height)
    fail("darktable segmentation returned no full-size mask");
  keep_seed_component(mask, mask_width, mask_height, args.threshold,
                      last_foreground_x, last_foreground_y);
  write_mask(args.mask_path, mask, mask_width, mask_height);
  write_paths(&args, mask, mask_width, mask_height);
  g_free(mask);
  dt_seg_free(segmentation);
  dt_ai_env_destroy(environment);
  dt_cleanup();
  return 0;
}
